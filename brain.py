"""
brain.py — Motor central de inteligencia de JARVIS 2.0.

STT:  OpenAI Whisper (local, sin API key)
LLM:  Ollama (local, sin API key)
TTS:  Edge-TTS (gratuito, sin autenticación)
Mem:  SQLite local

Patrón fire-and-confirm:
  - El TTS de confirmación se lanza en un thread paralelo ANTES de ejecutar la acción.
  - El usuario escucha respuesta mientras la acción ocurre en segundo plano.
  - Objetivo: feedback auditivo < 500ms desde fin del comando.

Nota sobre asyncio + Qt:
  - Edge-TTS usa aiohttp (async). Nunca llamar asyncio.run() desde el hilo Qt.
  - Crear un event loop nuevo por thread con asyncio.new_event_loop().
"""
import asyncio
import concurrent.futures
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import psutil
import pygame

from config import (
    DB_PATH,
    JARVIS_NAME,
    MAX_HISTORY_ENTRIES,
    MAX_HISTORY_MESSAGES,
    OLLAMA_BASE_URL,
    OLLAMA_CONTEXT_LENGTH,
    OLLAMA_MODEL,
    OLLAMA_NUM_GPU,
    OLLAMA_TIMEOUT_SEC,
    SAMPLE_RATE,
    SYSTEM_PROMPT,
    TTS_PITCH,
    TTS_RATE,
    TTS_VOICE,
    WHISPER_DEVICE,
    WHISPER_INITIAL_PROMPT,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
    USER_TITLE,
)

logger = logging.getLogger("jarvis.brain")

# Estados de JARVIS (deben coincidir con interface.py)
STATE_IDLE = "IDLE"
STATE_LISTENING = "LISTENING"
STATE_PROCESSING = "PROCESSING"
STATE_SPEAKING = "SPEAKING"


# ================================================================
# Memoria SQLite
# ================================================================

class JARVISMemory:
    """Gestión de memoria persistente en SQLite."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS conversations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp        TEXT    NOT NULL,
        user_input       TEXT    NOT NULL,
        jarvis_response  TEXT    NOT NULL,
        intent_type      TEXT,
        execution_result TEXT
    );
    CREATE TABLE IF NOT EXISTS preferences (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    _DEFAULTS = [
        ("user_name", USER_TITLE),
        ("last_opened_project", ""),
        ("wake_count", "0"),
    ]

    def __init__(self) -> None:
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        # WAL: lectores y escritores no se bloquean entre sí (StatsPanel lee
        # cada 2s mientras process_voice escribe). busy_timeout evita
        # 'database is locked' bajo concurrencia.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.executescript(self._SCHEMA)
        for key, value in self._DEFAULTS:
            self.conn.execute(
                "INSERT OR IGNORE INTO preferences VALUES (?,?,datetime('now'))",
                (key, value),
            )
        self.conn.commit()

    def save_conversation(
        self,
        user_input: str,
        response: str,
        intent: str = "conversation",
        result: str = "",
    ) -> None:
        """Guarda un turno de conversación y rota entradas antiguas."""
        self.conn.execute(
            "INSERT INTO conversations "
            "(timestamp,user_input,jarvis_response,intent_type,execution_result) "
            "VALUES (datetime('now'),?,?,?,?)",
            (user_input, response, intent, result),
        )
        self.conn.execute(
            "DELETE FROM conversations WHERE id NOT IN "
            "(SELECT id FROM conversations ORDER BY id DESC LIMIT ?)",
            (MAX_HISTORY_ENTRIES,),
        )
        self.conn.commit()

    def get_recent_context(self, n: int = MAX_HISTORY_MESSAGES) -> str:
        """Retorna los últimos n turnos formateados como texto para el LLM."""
        rows = self.conn.execute(
            "SELECT user_input, jarvis_response FROM conversations "
            "ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        if not rows:
            return "Sin historial previo."
        return "\n".join(
            f"Usuario: {r[0]}\nVIERNES: {r[1]}" for r in reversed(rows)
        )

    def search_conversations(self, days: int, min_days: int = 0) -> list[dict]:
        """Devuelve conversaciones dentro del rango de días indicado (hora local ~UTC)."""
        if min_days > 0:
            date_cond = (
                f"timestamp >= datetime('now', '-{days} days') "
                f"AND timestamp < datetime('now', '-{min_days} days')"
            )
        else:
            date_cond = f"timestamp >= datetime('now', '-{days} days')"
        rows = self.conn.execute(
            f"SELECT timestamp, user_input, jarvis_response, intent_type "
            f"FROM conversations WHERE {date_cond} ORDER BY id DESC LIMIT 25"
        ).fetchall()
        return [
            {"ts": r[0][:16], "input": r[1], "response": r[2], "intent": r[3]}
            for r in rows
        ]

    def get_preference(self, key: str) -> Optional[str]:
        """Lee una preferencia de la base de datos."""
        row = self.conn.execute(
            "SELECT value FROM preferences WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_preference(self, key: str, value: str) -> None:
        """Escribe o actualiza una preferencia."""
        self.conn.execute(
            "INSERT OR REPLACE INTO preferences VALUES (?,?,datetime('now'))",
            (key, value),
        )
        self.conn.commit()


# ================================================================
# Motor principal
# ================================================================

class JARVISBrain:
    """
    Motor central de JARVIS. Orquesta el pipeline completo:
    audio → STT → intención → LLM/acción → TTS.
    """

    # Keywords para detección de intención (evaluados en orden)
    INTENT_MAP: dict[str, list[str]] = {
        "activate_work_mode": [
            "a trabajar", "modo trabajo", "work mode", "empieza a trabajar",
            "vamos a trabajar", "empecemos", "activa modo trabajo",
            "quiero trabajar", "pon modo trabajo",
        ],
        "get_system_stats": [
            "estado del sistema", "cpu", "ram", "memoria", "rendimiento",
            "cómo está el sistema", "como esta el sistema",
            "cómo va la cpu", "cuanta memoria", "cuánta memoria",
            "cómo está el pc", "como esta el pc", "cómo está el ordenador",
            "estado del pc", "temperatura", "uso del sistema",
        ],
        "open_vscode": [
            "abre vscode", "abrir código", "abrir editor", "abre el editor",
            "abre vs code", "abrir visual studio", "abre visual studio",
            "abrir vscode", "pon el editor", "lanza el editor",
            "abre el código", "abre codigo",
        ],
        "open_terminal": [
            "abre terminal", "abrir terminal", "abre la terminal",
            "necesito la terminal", "abre consola", "abre la consola",
            "abrir consola", "abre powershell", "lanza terminal",
            "pon la terminal",
        ],
        "lock_screen": [
            "bloquea", "bloquear pantalla", "bloquea el equipo", "bloquear equipo",
            "bloquea el pc", "bloquea la pantalla", "me voy", "cierra sesión",
            "cierra sesion",
        ],
        "minimize_all": [
            "minimiza todo", "limpia pantalla", "minimiza las ventanas",
            "escritorio limpio", "minimiza", "despeja la pantalla",
            "muéstrame el escritorio", "muestrame el escritorio",
            "quita todo", "oculta todo",
        ],
        "close_distractions": [
            "cierra distracciones", "cierra todo", "modo enfoque", "enfoque",
            "cierra chrome", "cierra discord", "cierra spotify",
            "quita las distracciones", "sin distracciones",
            "modo concentración", "modo concentracion",
        ],
        "screenshot": [
            "captura de pantalla", "screenshot", "toma foto de pantalla",
            "captura pantalla", "haz una captura", "toma una captura",
            "foto de la pantalla", "captura esto",
        ],
        "volume_up": [
            "sube el volumen", "más volumen", "sube volumen", "volumen más alto",
            "aumenta el volumen", "más alto", "sube el audio",
        ],
        "volume_down": [
            "baja el volumen", "menos volumen", "baja volumen", "volumen más bajo",
            "baja el audio", "más bajo", "reduce el volumen",
        ],
        "volume_set": [
            "pon el volumen al", "volumen al", "ponlo al", "ponme el volumen",
            "volumen a", "coloca el volumen",
        ],
        "mute_volume": [
            "silencia", "silencio", "mutea", "sin sonido", "quita el sonido",
            "silencia el audio", "mute", "quita el audio",
        ],
        # play_song va ANTES de play_genre para que "pon X" no caiga en género
        "play_song": [
            "pon la canción", "reproduce la canción", "ponme la canción",
            "busca la canción", "pon el tema", "reproduce el tema",
            "quiero escuchar la canción", "pon la música de",
            "busca y pon", "reproduce esto",
        ],
        # play_playlist va ANTES de play_pause_media para que "playlist" no matchee "play"
        "play_playlist": [
            "pon mi playlist", "reproduce mi playlist", "abre mi playlist",
            "pon la playlist", "reproduce la playlist", "ponme la playlist",
            "pon mi lista de reproducción", "reproduce mi lista",
            "abre la playlist", "pon la lista de reproducción",
        ],
        "list_playlists": [
            "qué playlists tengo", "mis playlists", "lista mis playlists",
            "cuáles son mis playlists", "dime mis playlists",
            "qué listas de reproducción tengo",
        ],
        "play_pause_media": [
            "pausa", "pausa la música", "resume", "reanuda", "para la música",
            "continúa la música", "play", "pause", "para la canción",
        ],
        "open_chrome": [
            "abre chrome", "abre el navegador", "abre google chrome",
            "abrir chrome", "lanza chrome", "abre internet", "abre el internet",
        ],
        "open_apple_music": [
            "abre apple music", "pon apple music", "abre la música", "abrir apple music",
            "lanza apple music", "pon música", "abre music", "abre mi música",
            "pon mi música", "quiero escuchar música",
        ],
        "get_news": [
            "últimas noticias", "ultimas noticias", "qué ha pasado hoy",
            "que ha pasado hoy", "novedades", "noticias de hoy",
            "qué hay de nuevo en el mundo", "cuéntame las noticias",
            "cuentame las noticias", "noticias", "qué pasa en el mundo",
            "qué pasó hoy", "que paso hoy", "titulares de hoy",
        ],
        "get_crypto": [
            "precio del bitcoin", "precio del ethereum", "precio de",
            "cuánto vale el bitcoin", "cuanto vale el bitcoin",
            "cuánto vale el ethereum", "cuanto vale el ethereum",
            "cómo va el bitcoin", "como va el bitcoin", "criptomoneda",
            "cotización del bitcoin", "cotizacion del bitcoin",
            "cuánto vale la cripto", "cuanto vale el cripto",
            "precio de la criptomoneda", "cuánto está el bitcoin",
        ],
        "get_football": [
            "resultado del partido", "cómo quedó", "como quedo",
            "resultado del madrid", "resultado del barça", "resultado del barca",
            "cómo va el partido", "marcador", "quién ganó", "quien gano",
            "resultados de fútbol", "resultados de futbol",
            "clasificación de la liga", "clasificacion de la liga",
            "tabla de posiciones", "próximo partido", "proximo partido",
            "cuándo juega el madrid", "cuando juega el madrid",
        ],
        "web_search": [
            "busca en google", "busca en internet", "googlea", "búscame",
            "buscar en google", "busca el", "busca la", "busca los",
            "busca cuánto", "busca qué", "busca cómo", "busca cuándo",
            "busca información", "busca información sobre",
            "qué es", "quién es", "cuándo fue", "dónde está",
            "cuánto cuesta", "cuántos", "cuántas", "cómo se hace",
            "en qué año", "qué significa", "cómo funciona",
            "busca sobre", "infórmame sobre", "informame sobre",
        ],
        "enable_dnd": [
            "cállate", "callate", "silencio", "no me molestes", "modo silencio",
            "no hables", "activa el silencio", "activa modo silencio",
            "cállate un momento", "cállate por favor", "callate por favor",
            "silencio por", "silencio durante", "silencio hasta",
            "no quiero escucharte", "desactiva el sonido", "modo no molestar",
        ],
        "disable_dnd": [
            "ya puedes hablar", "desactiva el silencio", "sal del silencio",
            "reactívate", "reactivate", "vuelve a hablar", "puedes hablar",
            "desactiva modo silencio", "fin del silencio", "habla",
            "actívate", "activate", "quita el silencio",
        ],
        "set_reminder": [
            "recuérdame", "recuerdame", "pon un recordatorio", "ponme un recordatorio",
            "pon recordatorio", "acuérdame", "acuerdame", "avísame a las",
            "avisame a las", "recuérdame que", "recuerdame que",
            "ponme recordatorio", "necesito un recordatorio",
        ],
        "list_reminders": [
            "qué recordatorios tengo", "que recordatorios tengo",
            "mis recordatorios", "lista mis recordatorios",
            "cuáles son mis recordatorios", "tengo recordatorios pendientes",
            "recordatorios pendientes",
        ],
        "set_timer": [
            "pon un temporizador", "pon un timer", "ponme un timer",
            "temporizador de", "avísame en", "avísame dentro de",
            "timer de", "cuenta regresiva",
        ],
        "enable_autostart": [
            "activa el arranque automático", "activa arranque automático",
            "inicia con windows", "inicia solo", "arranca con windows",
            "pon el arranque automático", "que arranques solo",
            "activa el autostart", "autostart activado",
        ],
        "disable_autostart": [
            "desactiva el arranque automático", "desactiva arranque automático",
            "no arranques con windows", "no inicies solo",
            "quita el arranque automático", "desactiva el autostart",
            "que no arranques solo",
        ],
        # ── Control de archivos y aplicaciones ──────────────────────
        "open_app": [
            "abre la aplicación", "abre la app", "lanza la aplicación", "lanza la app",
            "ejecuta la aplicación", "ejecuta", "abre el programa", "lanza el programa",
            "inicia la app", "inicia el programa", "abre el juego", "lanza el juego",
            "pon en marcha", "arranca", "arranca el programa",
        ],
        "open_file": [
            "abre el archivo", "abre el fichero", "abre el documento",
            "abre la carpeta", "abrir el archivo", "abrir el documento",
            "abre el excel", "abre el word", "abre el pdf",
            "abre la imagen", "abre el video", "abre el vídeo",
            "abre la foto", "abre el proyecto",
        ],
        "read_file": [
            "lee el archivo", "lee el fichero", "lee el documento",
            "qué dice el archivo", "qué hay en el archivo",
            "muéstrame el archivo", "muéstrame el contenido",
            "leer el archivo", "leer el fichero",
        ],
        "create_file": [
            "crea un archivo", "crea un fichero", "crea un documento",
            "crea un txt", "nuevo archivo", "nuevo fichero",
            "crea un archivo de texto", "crea una nota",
        ],
        "rename_file": [
            "renombra el archivo", "renombra el fichero", "renombra el documento",
            "cambia el nombre del archivo", "cambia el nombre del fichero",
            "renombrar el archivo", "ponle el nombre",
        ],
        "move_file": [
            "mueve el archivo", "mueve el fichero", "mueve el documento",
            "mover el archivo", "traslada el archivo",
            "muévelo a", "mueve esto a",
        ],
        "copy_file": [
            "copia el archivo", "copia el fichero", "copia el documento",
            "copiar el archivo", "haz una copia",
            "duplica el archivo",
        ],
        "delete_file": [
            "borra el archivo", "borra el fichero", "borra el documento",
            "elimina el archivo", "elimina el fichero",
            "borrar el archivo", "eliminar el archivo",
            "manda a la papelera", "tira el archivo",
        ],
        "list_directory": [
            "qué hay en el escritorio", "qué hay en documentos",
            "lista el escritorio", "lista documentos",
            "muéstrame el escritorio", "muéstrame documentos",
            "qué archivos hay en", "lista los archivos de",
            "qué hay en descargas", "lista las descargas",
            "qué hay en la carpeta",
        ],
        "recent_files": [
            "archivos recientes", "qué he abierto últimamente",
            "últimos archivos", "ficheros recientes",
            "qué archivos he usado", "últimas cosas que abrí",
            "mis archivos recientes",
        ],
        "small_talk": [
            "cómo estás", "como estas", "cómo te encuentras", "qué tal estás",
            "que tal estas", "cómo vas", "como vas", "cómo te va", "como te va",
            "estás bien", "estas bien", "te encuentras bien", "qué hay de nuevo",
            "que hay de nuevo", "cómo te sientes", "como te sientes",
            "todo bien", "todo bien por ahí",
        ],
        "search_memory": [
            "qué te pedí", "que te pedi", "qué te pregunté", "que te pregunte",
            "qué hablamos", "que hablamos", "qué conversamos", "que conversamos",
            "recuerdas cuando", "recuerdas que", "qué dijiste", "que dijiste",
            "busca en el historial", "en qué quedamos", "en que quedamos",
            "qué te dije", "que te dije", "lo que te pedí", "lo que te pedi",
            "qué hice", "que hice", "qué abrí", "que abri",
        ],
        "read_notifications": [
            "qué notificaciones tengo", "léeme las notificaciones",
            "qué notificaciones hay", "tengo notificaciones",
            "mis notificaciones", "lee las notificaciones",
            "qué me han notificado", "notificaciones recientes",
            "qué ha pasado", "qué me perdí", "hay algo nuevo",
            "alguna notificación", "revisa las notificaciones",
        ],
        "read_clipboard": [
            "qué tengo copiado", "que tengo copiado", "qué hay en el portapapeles",
            "lee el portapapeles", "lee lo que tengo copiado",
            "portapapeles", "qué copié", "que copie",
        ],
        "translate_clipboard": [
            "traduce esto", "traduce lo que tengo copiado", "traduce el portapapeles",
            "traduce lo que copié", "traduce", "translate this",
        ],
        "correct_clipboard": [
            "corrige esto", "corrige el texto", "corrige lo que tengo copiado",
            "corrígelo", "corrige mi texto", "revisa la gramática",
            "mejora esto", "mejora el texto", "corrige lo que copié",
        ],
        # ── Música avanzada ─────────────────────────────────────────
        "next_track": [
            "siguiente canción", "siguiente tema", "siguiente pista",
            "salta esta", "salta la canción", "próxima canción",
            "pon la siguiente", "siguiente", "skip",
        ],
        "prev_track": [
            "canción anterior", "tema anterior", "vuelve atrás",
            "anterior", "la de antes", "vuelve a la anterior",
            "repite la anterior",
        ],
        "stop_media": [
            "para la música", "para el reproductor", "detén la música",
            "detener música", "para todo",
        ],
        "now_playing": [
            "qué está sonando", "qué canción es", "qué música suena",
            "qué está reproduciendo", "cómo se llama esta canción",
            "cómo se llama esta", "quién canta esto", "qué canción suena",
            "dime qué suena", "qué está pasando en spotify",
            "nombre de la canción", "qué es esto que suena",
        ],
        "play_genre": [
            "ponme", "pon música de", "quiero escuchar", "ponme música de",
            "reproduce", "pon algo de", "ponme algo de",
            "quiero oir", "quiero oír", "pon", "música de",
        ],
        "like_track": [
            "me gusta esta canción", "márcala como favorita", "dale like",
            "añade a favoritos", "me gusta esto", "marca esta canción",
            "pon corazón", "ponle like",
        ],
    }

    # Respuestas de confirmación inmediatas (fire-and-confirm)
    _ACTION_FEEDBACK: dict[str, str] = {
        "activate_work_mode": (
            "Activando su entorno de trabajo, Señor. "
            "Espero que esta vez sí lo use."
        ),
        "open_vscode": "Abriendo el editor, Señor. Un entorno digno, al menos.",
        "open_terminal": "Abriendo la terminal. Trate de no romper nada.",
        "lock_screen": "Bloqueando la pantalla. Descanso bien merecido, supongo.",
        "minimize_all": "Minimizando todo. Pantalla limpia, mente limpia... ojalá.",
        "close_distractions": (
            "Cerrando sus fuentes de distracción habituales, Señor."
        ),
        "get_system_stats": "Consultando el estado del sistema. Un momento.",
        "screenshot": "Capturando pantalla, Señor.",
        "volume_up": "Subiendo el volumen, Señor.",
        "volume_down": "Bajando el volumen.",
        "volume_set": "Ajustando el volumen, Señor.",
        "mute_volume": "Audio silenciado.",
        "play_pause_media": "Hecho.",
        "open_chrome": "Abriendo Chrome, Señor.",
        "open_apple_music": "Abriendo Apple Music, Señor.",
        "web_search": "Buscando en internet, Señor.",
        "get_news": "Consultando las últimas noticias, Señor.",
        "get_crypto": "Consultando el precio, un momento.",
        "get_football": "Buscando los resultados, Señor.",
        "enable_dnd": "",   # No feedback — VIERNES se calla inmediatamente
        "disable_dnd": "",  # El handler habla directamente
        "set_reminder": "Programando el recordatorio, Señor.",
        "list_reminders": "Consultando sus recordatorios.",
        "set_timer": "Temporizador activado, Señor.",
        "enable_autostart": "Configurando arranque automático con Windows, Señor.",
        "disable_autostart": "Desactivando arranque automático, Señor.",
        # Archivos y apps
        "open_app": "Buscando y abriendo la aplicación, Señor.",
        "open_file": "Buscando y abriendo el archivo, Señor.",
        "read_file": "Leyendo el archivo, un momento.",
        "create_file": "Creando el archivo, Señor.",
        "rename_file": "Renombrando el archivo, Señor.",
        "move_file": "Moviendo el archivo, Señor.",
        "copy_file": "Copiando el archivo, Señor.",
        "delete_file": "Enviando el archivo a la papelera, Señor.",
        "list_directory": "Revisando el contenido de la carpeta.",
        "recent_files": "Consultando sus archivos recientes.",
        "search_memory": "Revisando el historial, Señor. Un momento.",
        "read_notifications": "Revisando sus notificaciones, Señor.",
        "read_clipboard": "Leyendo el portapapeles, Señor.",
        "translate_clipboard": "Traduciendo el contenido, Señor. Un momento.",
        "correct_clipboard": "Revisando la gramática, Señor. Un momento.",
        # Música
        "small_talk": "",   # La respuesta real se genera en _dispatch_action
        "next_track": "Siguiente canción.",
        "prev_track": "Canción anterior.",
        "stop_media": "Deteniendo la reproducción.",
        "now_playing": "Consultando qué está sonando.",
        "play_song": "Buscando la canción en Apple Music, Señor.",
        "play_genre": "Buscando música, Señor.",
        "play_playlist": "Abriendo la playlist, Señor.",
        "list_playlists": "Consultando sus playlists.",
        "like_track": "Marcando como favorita.",
    }

    # Intents destructivos: requieren confirmación verbal antes de ejecutarse.
    _CONFIRM_INTENTS: frozenset = frozenset({"delete_file", "move_file", "rename_file"})

    # Palabras que cuentan como "sí" en una confirmación (el resto = cancelar).
    _AFFIRMATIVE_WORDS: frozenset = frozenset({
        "si", "sí", "claro", "confirmo", "confirmado", "adelante", "hazlo",
        "dale", "correcto", "afirmativo", "ok", "okay", "vale", "eso", "exacto",
        "por supuesto", "procede", "acepto", "sip", "yes",
    })

    def __init__(
        self,
        on_status_change: Optional[Callable[[str], None]] = None,
        on_message: Optional[Callable[[str, str], None]] = None,
        on_task_logged: Optional[Callable[[str], None]] = None,
        on_waveform_update: Optional[Callable[[list], None]] = None,
    ) -> None:
        self.memory = JARVISMemory()
        self.on_status_change = on_status_change or (lambda s: None)
        self.on_message = on_message or (lambda s, t: None)
        self.on_task_logged = on_task_logged or (lambda t: None)
        self.on_waveform_update = on_waveform_update or (lambda d: None)

        self._whisper_model = None
        self._whisper_backend: Optional[str] = None
        self._pygame_initialized = False
        self._is_processing: bool = False
        self._whisper_lock = threading.Lock()   # Whisper no es thread-safe en GPU
        self._tts_lock = threading.Lock()       # Solo un TTS a la vez — evita solapamiento
        self._stop_speaking = threading.Event()  # Interrumpir TTS en curso
        self._on_keyword_wake_cb: Optional[Callable[[], None]] = None
        self._on_listen_again_cb: Optional[Callable[[], None]] = None  # re-escuchar sin wake word
        self._silent_until: Optional[float] = None  # timestamp Unix hasta el que VIERNES calla
        # Diálogo de confirmación para acciones destructivas (borrar/mover/renombrar)
        self._pending_confirmation: Optional[dict] = None
        # Protege _is_processing y _pending_confirmation entre threads
        # (process_voice, watcher de notificaciones, captura de respuestas)
        self._state_lock = threading.Lock()
        self._load_whisper()
        self._init_pygame()
        self._start_notification_watcher()
        self._start_reminder_manager()
        logging.getLogger("asyncio").setLevel(logging.WARNING)

        # Loop asyncio persistente — un único event loop compartido por todos los
        # hilos TTS. Elimina el overhead de ~15ms por llamada a _tts_bytes que
        # antes pagábamos con asyncio.new_event_loop() + loop.close().
        self._async_loop = asyncio.new_event_loop()
        threading.Thread(
            target=self._async_loop.run_forever,
            daemon=True,
            name="async_tts_loop",
        ).start()

        # Executor para generación TTS concurrente en _stream_and_speak.
        # 2 workers: chunk N+1 se genera mientras chunk N se reproduce.
        self._tts_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="tts_gen"
        )

        # Semantic router — inicializado en background para no bloquear arranque
        self._semantic_router = None
        threading.Thread(
            target=self._init_semantic_router,
            daemon=True,
            name="router_init",
        ).start()

        # Pre-calentar Edge-TTS: establece la conexión TCP con Microsoft para que
        # la primera respuesta real no pague el coste de handshake (~300ms en frío).
        threading.Thread(target=self._warmup_tts, daemon=True, name="tts_warmup").start()

    # ----------------------------------------------------------------
    # Inicialización
    # ----------------------------------------------------------------

    def _init_semantic_router(self) -> None:
        """Carga el SemanticRouter en background para no bloquear el arranque."""
        try:
            from semantic_router import SemanticRouter
            self._semantic_router = SemanticRouter(self.INTENT_MAP)
        except Exception as e:
            logger.warning(f"SemanticRouter no disponible: {e}")

    def _start_reminder_manager(self) -> None:
        """Inicializa el gestor de recordatorios y recarga los pendientes."""
        try:
            from reminder_manager import ReminderManager
            self._reminder_manager = ReminderManager(
                db_path=DB_PATH,
                on_fire=self._on_reminder_fire,
            )
            logger.info("ReminderManager inicializado.")
        except Exception as e:
            self._reminder_manager = None
            logger.warning(f"No se pudo iniciar ReminderManager: {e}")

    def _on_reminder_fire(self, message: str) -> None:
        """Callback cuando salta un recordatorio — habla si no está procesando."""
        text = f"Señor, recordatorio: {message}."
        self.on_message(JARVIS_NAME, text)
        threading.Thread(
            target=self._speak_sync,
            args=(text,),
            daemon=True,
            name="reminder_tts",
        ).start()

    def _start_notification_watcher(self) -> None:
        """Inicia el monitor de notificaciones en tiempo real."""
        try:
            from notification_watcher import NotificationWatcher
            self._notif_watcher = NotificationWatcher(
                on_notification=self._on_new_notification
            )
            self._notif_watcher.start()
        except Exception as e:
            logger.warning(f"No se pudo iniciar el watcher de notificaciones: {e}")

    def _on_new_notification(self, msg: str) -> None:
        """Callback del watcher: habla la notificación si VIERNES no está ocupado."""
        with self._state_lock:
            busy = self._is_processing or self._pending_confirmation is not None
        # No interrumpir un comando en curso, una confirmación pendiente ni el modo DND
        if busy or self._is_silent_mode():
            return
        threading.Thread(
            target=self._speak_sync,
            args=(msg,),
            daemon=True,
            name="notif_tts",
        ).start()

    def _load_whisper(self) -> None:
        """Carga faster-whisper con cuantización INT8 (3-4× más rápido que Whisper estándar)."""
        try:
            from faster_whisper import WhisperModel

            device = WHISPER_DEVICE if WHISPER_DEVICE in ("cuda", "cpu") else "cpu"
            compute = "int8_float16" if device == "cuda" else "int8"

            self._whisper_model = WhisperModel(
                WHISPER_MODEL,
                device=device,
                compute_type=compute,
                num_workers=1,
            )
            self._whisper_backend = "faster"
            logger.info(f"faster-whisper cargado en {device.upper()} ({compute}): {WHISPER_MODEL}")
        except Exception as e:
            logger.error(f"Error cargando faster-whisper: {e}")
            self._whisper_model = None
            self._whisper_backend = None

    def _init_pygame(self) -> None:
        """Inicializa pygame.mixer para reproducción de TTS y beeps."""
        try:
            pygame.mixer.pre_init(frequency=22050, size=-16, channels=2, buffer=512)
            pygame.mixer.init()
            self._pygame_initialized = True
            logger.info("pygame.mixer inicializado.")
        except Exception as e:
            logger.error(f"Error inicializando pygame.mixer: {e}")

    # Frases de activación — sarcásticas, cortas, < 1.5s de audio
    _WAKE_PHRASES: list[str] = [
        "Dígame, Señor.",
        "A sus órdenes.",
        "¿Qué desea?",
        "¿Me llamaba?",
        "Aquí presente.",
        "¿Sí, Señor?",
        "¿En qué puedo servirle?",
        "Escucho.",
    ]

    def _warmup_tts(self) -> None:
        """
        Pre-genera audio MP3 para todas las frases de activación y las
        almacena en caché. Corre en background al arrancar.
        La primera llamada también pre-calienta la conexión Edge-TTS.
        """
        import random
        self._wake_audio_cache: list[bytes] = []
        for phrase in self._WAKE_PHRASES:
            try:
                audio = self._tts_bytes(phrase)
                if audio:
                    self._wake_audio_cache.append(audio)
            except Exception as e:
                logger.debug(f"TTS warmup frase '{phrase}': {e}")
        logger.info(
            f"Edge-TTS pre-calentado. "
            f"{len(self._wake_audio_cache)}/{len(self._WAKE_PHRASES)} frases en caché."
        )

    def play_wake_phrase(self) -> None:
        """
        Reproduce una frase de activación aleatoria (pre-cacheada).
        Bloqueante — retorna cuando el audio termina, garantizando que
        el listener no capture la voz de VIERNES.
        Si la caché aún no está lista, reproduce un beep corto de fallback.
        """
        if self._is_silent_mode():
            return

        # Intentar frase pre-cacheada
        cache = getattr(self, "_wake_audio_cache", [])
        if cache and self._pygame_initialized:
            import random
            audio = random.choice(cache)
            self._play_audio_bytes(audio)
            return

        # Fallback: beep corto mientras la caché se genera
        if not self._pygame_initialized:
            return
        try:
            sample_rate = 22050
            for freq, duration in [(880, 0.08), (1320, 0.12)]:
                t = np.linspace(0, duration, int(sample_rate * duration), False)
                wave = (np.sin(2 * np.pi * freq * t) * 0.6 * 32767).astype(np.int16)
                stereo = np.column_stack([wave, wave])
                sound = pygame.sndarray.make_sound(stereo)
                sound.set_volume(1.0)
                channel = sound.play()
                while channel and channel.get_busy():
                    pygame.time.wait(10)
        except Exception as e:
            logger.debug(f"Wake fallback beep error: {e}")

    # ----------------------------------------------------------------
    # Helper de inferencia Whisper (faster-whisper API)
    # ----------------------------------------------------------------

    def _whisper_transcribe(
        self,
        audio: np.ndarray,
        beam_size: int = 1,
        use_prompt: bool = True,
    ) -> str:
        """
        Ejecuta faster-whisper y retorna el texto completo.
        Siempre llamar con _whisper_lock adquirido.

        use_prompt=False en keyword check: evita que Whisper alucinó el
        initial_prompt cuando recibe ruido/silencio y detecte wake words
        que él mismo inventó.
        """
        segments, info = self._whisper_model.transcribe(
            audio.astype(np.float32),
            language=WHISPER_LANGUAGE,
            beam_size=beam_size,
            condition_on_previous_text=False,
            initial_prompt=WHISPER_INITIAL_PROMPT if use_prompt else None,
            vad_filter=True,           # Filtro VAD interno — ignora silencio automáticamente
            vad_parameters={"min_silence_duration_ms": 300},
            no_speech_threshold=0.85,  # Descartar solo si Whisper está muy seguro de que no hay voz
        )
        # Si la probabilidad de "no hay voz" es muy alta, retornar vacío
        if hasattr(info, "no_speech_prob") and info.no_speech_prob > 0.85:
            return ""
        return " ".join(s.text.strip() for s in segments).strip()

    # ----------------------------------------------------------------
    # Detección de palabras clave de activación
    # ----------------------------------------------------------------

    def check_wake_keyword(self, audio: np.ndarray) -> None:
        """
        Transcribe un segmento de audio corto y comprueba si contiene
        una palabra clave de activación. Si la detecta, llama a on_wake_keyword_cb.
        Llamar desde un thread separado — nunca desde el hilo Qt ni el listener loop.
        """
        from config import WAKE_KEYWORDS

        if self._whisper_model is None:
            return

        if self._is_processing:
            return

        if len(audio) < 6400:   # < 0.4s — demasiado corto para ser una keyword
            return

        # Solo una inferencia Whisper a la vez — la GPU no es thread-safe
        if not self._whisper_lock.acquire(blocking=False):
            return

        try:
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms < 0.003:   # Umbral más alto: solo procesar audio con voz real
                return

            # Padear a mínimo 1s — evita reshape errors en Whisper medium
            if len(audio) < SAMPLE_RATE:
                audio = np.pad(audio, (0, SAMPLE_RATE - len(audio)))

            text = self._whisper_transcribe(audio, beam_size=1, use_prompt=False).lower()
            logger.info(f"[KW] Whisper: '{text}'")

            for kw in WAKE_KEYWORDS:
                if kw in text:
                    logger.info(f"Palabra clave detectada: '{kw}' en '{text}'")
                    # Interrumpir TTS si está hablando
                    self._interrupt_speaking()
                    if self._on_keyword_wake_cb:
                        self._on_keyword_wake_cb()
                    break
        except Exception as e:
            logger.debug(f"Error en keyword check: {e}")
        finally:
            self._whisper_lock.release()   # Siempre liberar, pase lo que pase

    def set_on_keyword_wake_callback(self, callback: Callable[[], None]) -> None:
        """Callback a llamar cuando se detecta una palabra clave de activación."""
        self._on_keyword_wake_cb = callback

    def set_on_listen_again_callback(self, callback: Callable[[], None]) -> None:
        """Callback para re-escuchar SIN wake word (respuestas de confirmación)."""
        self._on_listen_again_cb = callback

    # ----------------------------------------------------------------
    # Confirmación verbal de acciones destructivas
    # ----------------------------------------------------------------

    def _speak_and_log(self, user_text: str, msg: str, intent: str) -> None:
        """Muestra, guarda y dice un mensaje (helper para respuestas simples)."""
        self.on_message(JARVIS_NAME, msg)
        self.memory.save_conversation(user_text, msg, intent)
        self._speak_sync(msg)

    def _trigger_listen_again(self) -> None:
        """Reactiva la captura de voz para oír la respuesta a una confirmación."""
        cb = self._on_listen_again_cb or self._on_keyword_wake_cb
        if cb:
            try:
                cb()
            except Exception as e:
                logger.debug(f"listen_again error: {e}")

    def _request_file_confirmation(self, intent: str, original_text: str) -> None:
        """
        Acción destructiva: localiza el archivo objetivo, pide confirmación
        verbal y deja la acción en espera hasta recibir el sí/no.
        """
        from file_manager import find_file

        if intent == "delete_file":
            kwargs = {"file_name": self._extract_file_name(original_text)}
            verb = "enviar a la papelera"
        elif intent == "move_file":
            file_name, destination = self._extract_move_params(original_text)
            kwargs = {"file_name": file_name, "destination": destination}
            verb = f"mover a {destination}"
        elif intent == "rename_file":
            file_name, new_name = self._extract_rename_params(original_text)
            kwargs = {"file_name": file_name, "new_name": new_name}
            verb = f"renombrar a {new_name}"
        else:
            return

        if not kwargs.get("file_name"):
            self._speak_and_log(original_text, "No entendí qué archivo, Señor.", intent)
            return
        if intent == "rename_file" and not kwargs.get("new_name"):
            self._speak_and_log(original_text, "¿A qué nombre lo renombro, Señor?", intent)
            return

        # Localizar el archivo real para nombrarlo con precisión en la pregunta
        results = find_file(kwargs["file_name"]) or find_file(kwargs["file_name"], full_disk=True)
        if not results:
            self._speak_and_log(
                original_text,
                f"No encontré ningún archivo llamado '{kwargs['file_name']}', Señor.",
                intent,
            )
            return

        target = results[0]
        question = (
            f"¿Confirma que desea {verb} el archivo {target.name}, Señor? Diga sí o no."
        )
        self.on_message(JARVIS_NAME, question)
        with self._state_lock:
            self._pending_confirmation = {
                "intent": intent,
                "kwargs": kwargs,
                "target_name": target.name,
                "ts": time.time(),
            }
        self._speak_sync(question)
        self._trigger_listen_again()   # Re-escuchar la respuesta sin wake word

    def _handle_confirmation(self, text: str) -> bool:
        """
        Interpreta la respuesta a una confirmación pendiente.
        Retorna True si consumió el texto (sí/no claro); False si fue ambiguo
        o expiró, en cuyo caso se cancela la acción y el texto sigue su curso
        como comando normal.
        """
        with self._state_lock:
            pending = self._pending_confirmation
            self._pending_confirmation = None   # pop atómico
        if not pending:
            return False

        # Expirada (>30s) → no consumir; procesar el texto como comando normal
        if time.time() - pending.get("ts", 0) > 30:
            logger.info("Confirmación expirada; el texto se procesa como comando normal.")
            return False

        def _strip(s: str) -> str:
            s = unicodedata.normalize("NFD", s.lower())
            return "".join(c for c in s if unicodedata.category(c) != "Mn")

        words = set(re.findall(r"\w+", _strip(text)))
        affirmative = bool(words & {_strip(w) for w in self._AFFIRMATIVE_WORDS})
        negative = bool(words & {
            "no", "cancela", "cancelar", "para", "nada", "olvidalo",
            "dejalo", "negativo", "mejor", "tampoco", "espera",
        })

        # Ambiguo (ni sí ni no) → cancelar acción por seguridad y seguir como comando
        if not affirmative and not negative:
            logger.info("Respuesta de confirmación ambigua; acción destructiva cancelada.")
            return False

        if affirmative and not negative:
            from actions import ACTIONS
            intent = pending["intent"]
            action_fn = ACTIONS.get(intent)
            try:
                result = (
                    action_fn(**pending["kwargs"]) if action_fn
                    else {"success": False, "message": "Acción no disponible, Señor."}
                )
            except Exception as e:
                logger.error(f"Error ejecutando acción confirmada '{intent}': {e}")
                result = {"success": False, "message": "Hubo un error al ejecutar la acción, Señor."}
            msg = result.get("message", "Hecho, Señor.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(f"[confirmado] {intent}", msg, intent, str(result))
            self.on_task_logged(intent.replace("_", " ").title())
            self._speak_sync(msg)
        else:
            msg = "Cancelado, Señor. No he tocado nada."
            self.on_message(JARVIS_NAME, msg)
            self._speak_sync(msg)
        return True

    # ----------------------------------------------------------------
    # Pipeline principal
    # ----------------------------------------------------------------

    def process_voice(self, audio: np.ndarray) -> None:
        """
        Pipeline completo: audio numpy → texto → intención → respuesta → TTS.
        Llamar desde un thread separado (nunca desde el hilo Qt).
        """
        if self._whisper_model is None:
            self._speak_sync(
                "Parece que mi módulo de transcripción no está disponible, Señor. "
                "Verifique la instalación de Whisper."
            )
            return

        with self._state_lock:
            self._is_processing = True
        self._stop_speaking.clear()   # Reset interrupción para este comando
        self.on_status_change(STATE_PROCESSING)

        try:
            # 1. STT — Transcribir audio con Whisper
            text = self._transcribe(audio)
            if not text:
                return

            self.on_message("Tú", text)

            # Confirmación pendiente de una acción destructiva → interpretar sí/no
            with self._state_lock:
                has_pending = self._pending_confirmation is not None
            if has_pending:
                if self._handle_confirmation(text):
                    return
                # Respuesta ambigua/expirada → seguir procesando como comando normal

            # 2. Detectar intención
            intent = self._detect_intent(text)

            # 3. Ejecutar según intención
            if intent in self._CONFIRM_INTENTS:
                # Acción destructiva → pedir confirmación verbal antes de ejecutar
                self._request_file_confirmation(intent, text)
            elif intent == "web_search":
                from actions import ACTIONS
                query = self._extract_search_query(text)
                self._speak_sync("Buscando información, un momento, Señor.")
                result = ACTIONS["web_search"](query=query)
                if result["success"] and result["message"]:
                    data = result.get("data") or {}
                    if data.get("realtime"):
                        summary = result["message"]
                    else:
                        summary = self._summarize_web_content(query, result["message"])
                    self.on_message(JARVIS_NAME, summary)
                    self.memory.save_conversation(text, summary, "web_search")
                    # Abrir Chrome con Google search en paralelo al TTS
                    threading.Thread(
                        target=self._open_search_in_chrome,
                        args=(query,),
                        daemon=True,
                        name="jarvis_chrome",
                    ).start()
                    self._speak_sync(summary)
                else:
                    self._speak_sync(result.get("message", "No encontré resultados para eso."))

            elif intent:
                from actions import ACTIONS
                action_fn = ACTIONS.get(intent)
                if action_fn:
                    feedback = self._ACTION_FEEDBACK.get(
                        intent, f"Ejecutando {intent.replace('_', ' ')}, Señor."
                    )
                    tts_thread = threading.Thread(
                        target=self._speak_sync,
                        args=(feedback,),
                        daemon=True,
                        name="jarvis_tts",
                    )
                    tts_thread.start()

                    action_thread = threading.Thread(
                        target=self._execute_action,
                        args=(intent, action_fn, text),
                        daemon=True,
                        name="jarvis_action",
                    )
                    action_thread.start()
                    tts_thread.join()
            else:
                # Pre-routing: si la pregunta necesita datos actuales, ir directo a web
                if self._query_needs_internet(text):
                    logger.info("Pre-routing a web_search (query con datos actuales)")
                    from actions import ACTIONS
                    self._speak_sync("Buscando en internet, Señor.")
                    result = ACTIONS["web_search"](query=text)
                    if result.get("success") and result.get("message"):
                        summary = self._summarize_web_content(text, result["message"])
                        self.on_message(JARVIS_NAME, summary)
                        self.memory.save_conversation(text, summary, "web_search")
                        threading.Thread(
                            target=self._open_search_in_chrome,
                            args=(text,), daemon=True, name="jarvis_chrome",
                        ).start()
                        self._speak_sync(summary)
                    else:
                        response = self._stream_and_speak(text)
                        self.memory.save_conversation(text, response)
                else:
                    response = self._stream_and_speak(text)
                    self.memory.save_conversation(text, response)
        finally:
            with self._state_lock:
                self._is_processing = False
            self.on_status_change(STATE_IDLE)

    # ----------------------------------------------------------------
    # STT
    # ----------------------------------------------------------------

    # Alucinaciones conocidas de Whisper en español con audio corto/ruidoso.
    # Si el resultado coincide exactamente con alguna, se descarta como inválido.
    _WHISPER_HALLUCINATIONS: frozenset = frozenset({
        "¡viva!", "viva", "gracias.", "gracias", "sí.", "sí", "si.", "si",
        "no.", "no", "ok.", "ok", "bien.", "bien", "hola.", "hola",
        "adiós.", "adios.", "adiós", "adios",
        "subtítulos realizados por la comunidad de amara.org",
        "subtitulos realizados por la comunidad de amara.org",
        "amara.org", "www.movistar.es", ".", "..", "...", "…",
        "transcripción automática.", "transcripcion automatica.",
        "música", "musica", "♪", "♫",
    })

    def _transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio con Whisper. Retorna string vacío en caso de error."""
        rms_total = float(np.sqrt(np.mean(audio ** 2)))
        if rms_total < 0.0001:   # Solo descarta silencio absoluto
            logger.info(f"Audio descartado (RMS={rms_total:.5f})")
            return ""

        # Padear a mínimo 2s para evitar errores de reshape con Whisper medium
        min_samples = SAMPLE_RATE * 2   # 32000 muestras = 2s
        if len(audio) < min_samples:
            audio = np.pad(audio, (0, min_samples - len(audio)))

        try:
            with self._whisper_lock:   # Uso exclusivo de la GPU
                t0 = time.time()
                text = self._whisper_transcribe(audio, beam_size=1)
            elapsed = time.time() - t0
            logger.info(f"Transcripción ({elapsed:.2f}s): '{text}'")

            # Filtrar alucinaciones conocidas de Whisper
            if text.lower().strip() in self._WHISPER_HALLUCINATIONS:
                logger.info(f"Alucinación de Whisper descartada: '{text}'")
                return ""

            return text
        except Exception as e:
            logger.error(f"Error en STT: {e}")
            self._speak_sync(
                "No he logrado descifrar eso, Señor. "
                "Quizás intente con palabras esta vez."
            )
            return ""

    # ----------------------------------------------------------------
    # Detección de intención
    # ----------------------------------------------------------------

    def _detect_intent(self, text: str) -> Optional[str]:
        """
        Pipeline de detección de intención en 2 etapas (sin LLM → más rápido):

        Stage 1 (<1ms):  keyword matching exacto/substring con normalización de acentos.
        Stage 2 (<5ms):  TF-IDF coseno via SemanticRouter — captura paráfrasis y
                         variaciones que no están en el INTENT_MAP.

        Si ninguna etapa decide, retorna None → conversación libre con Ollama.
        (El antiguo Stage 3 con llama3.2:1b se eliminó: añadía ~400ms y, al ser un
        modelo de 1B, devolvía intents equivocados — fallaba más de lo que acertaba.)
        """
        def _strip_accents(s: str) -> str:
            s = unicodedata.normalize("NFD", s)
            return "".join(c for c in s if unicodedata.category(c) != "Mn")

        text_lower = _strip_accents(text.lower())

        def _matches(kw: str) -> bool:
            kw_norm = _strip_accents(kw.lower())
            if len(kw_norm) <= 6:
                return bool(re.search(r'\b' + re.escape(kw_norm) + r'\b', text_lower))
            return kw_norm in text_lower

        # Stage 1 — keyword matching (siempre primero: más rápido y más preciso)
        for intent, keywords in self.INTENT_MAP.items():
            if any(_matches(kw) for kw in keywords):
                logger.info(f"[S1-KW] Intención: {intent}")
                return intent

        # Stage 2 — semantic router TF-IDF
        if self._semantic_router is not None:
            intent, score = self._semantic_router.route(text)
            if intent:
                logger.info(f"[S2-SEM] Intención: {intent} (score={score:.3f})")
                return intent

        return None

    # ----------------------------------------------------------------
    # Ejecución de acciones
    # ----------------------------------------------------------------

    def _execute_action(
        self, intent: str, action_fn: Callable, original_text: str
    ) -> None:
        """Ejecuta la acción del sistema y registra el resultado en memoria."""
        try:
            result = self._dispatch_action(intent, action_fn, original_text)
            if result is None:
                return  # ya gestionado internamente (web_search, read_file, etc.)

            msg = result.get("message", "Hecho.")
            self.memory.save_conversation(original_text, msg, intent, str(result))
            self.on_task_logged(intent.replace("_", " ").title())

            # Para acciones que devuelven info útil — esperar a que el feedback TTS
            # termine (el lock lo garantiza) y luego hablar el resultado real
            if intent in ("list_directory", "recent_files", "get_system_stats", "read_notifications") and result.get("success"):
                # El _tts_lock en _speak_sync garantiza que espera al feedback previo
                self._speak_sync(msg)
        except Exception as e:
            logger.error(f"Error en acción '{intent}': {e}")

    def _dispatch_action(
        self, intent: str, action_fn: Callable, original_text: str
    ) -> Optional[dict]:
        """
        Extrae parámetros del texto y llama a la acción con los argumentos correctos.
        Retorna el resultado o None si la acción se gestionó completamente aquí.
        """
        # ── Volumen ──
        if intent == "set_timer":
            return action_fn(seconds=self._extract_seconds(original_text))
        if intent == "volume_up":
            return action_fn(amount=self._extract_percent(original_text) or 20)
        if intent == "volume_down":
            return action_fn(amount=self._extract_percent(original_text) or 20)
        if intent == "volume_set":
            return action_fn(percent=self._extract_percent(original_text) or 50)

        # Nota: web_search se gestiona en process_voice antes de llegar aquí,
        # por eso no hay rama para él en este dispatcher (evita duplicación).

        # ── Archivos y aplicaciones ──────────────────────────────
        if intent == "open_app":
            app_name = self._extract_app_name(original_text)
            return action_fn(app_name=app_name)

        if intent == "open_file":
            file_name = self._extract_file_name(original_text)
            return action_fn(file_name=file_name)

        if intent == "read_file":
            file_name = self._extract_file_name(original_text)
            result = action_fn(file_name=file_name)
            if result["success"] and result.get("data"):
                content = result["data"].get("content", "")
                preview = content[:800]
                # Resumir el contenido con Ollama
                summary = self._summarize_file_content(file_name, preview)
                self.on_message(JARVIS_NAME, summary)
                self.memory.save_conversation(original_text, summary, "read_file")
                self._speak_sync(summary)
            else:
                msg = result.get("message", "No pude leer el archivo.")
                self._speak_sync(msg)
            return None

        if intent == "create_file":
            file_name, location = self._extract_create_file_params(original_text)
            return action_fn(file_name=file_name, location=location)

        if intent == "rename_file":
            old_name, new_name = self._extract_rename_params(original_text)
            return action_fn(file_name=old_name, new_name=new_name)

        if intent == "move_file":
            file_name, destination = self._extract_move_params(original_text)
            return action_fn(file_name=file_name, destination=destination)

        if intent == "copy_file":
            file_name, destination = self._extract_move_params(original_text)
            return action_fn(file_name=file_name, destination=destination)

        if intent == "delete_file":
            file_name = self._extract_file_name(original_text)
            return action_fn(file_name=file_name)

        if intent == "list_directory":
            location = self._extract_location(original_text)
            return action_fn(location=location)

        # ── Música ───────────────────────────────────────────────────
        if intent == "now_playing":
            result = action_fn()
            msg = result.get("message", "No hay nada sonando.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "now_playing")
            self._speak_sync(msg)
            return None

        if intent == "play_song":
            song = self._extract_song_name(original_text)
            result = action_fn(name=song)
            msg = result.get("message", "Hecho.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "play_song")
            if not result.get("success"):
                self._speak_sync(msg)
            return None

        if intent == "play_genre":
            genre = self._extract_genre(original_text)
            return action_fn(genre=genre)

        if intent == "play_playlist":
            name = self._extract_playlist_name(original_text)
            result = action_fn(name=name)
            msg = result.get("message", "Hecho.")
            if not result.get("success"):
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None
            return result

        if intent == "read_notifications":
            result = action_fn()
            msg = result.get("message", "No hay notificaciones.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "read_notifications")
            self._speak_sync(msg)
            return None

        if intent == "search_memory":
            date_info = self._parse_memory_date(original_text)
            rows = self.memory.search_conversations(date_info["days"], date_info.get("min_days", 0))
            if not rows:
                msg = f"No encuentro conversaciones de {date_info['label']}, Señor."
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None

            # Detección de intent específico en la pregunta → respuesta directa sin LLM
            q = original_text.lower()
            direct_msg = self._search_memory_direct(q, rows)
            if direct_msg:
                self.on_message(JARVIS_NAME, direct_msg)
                self.memory.save_conversation(original_text, direct_msg, "search_memory")
                self._speak_sync(direct_msg)
                return None

            # Fallback: LLM solo con lo que está textualmente en el historial
            history = "\n".join(
                f"[{r['ts']}] Usuario: {r['input']}\nVIERNES: {r['response']}"
                for r in rows[:10]
            )
            prompt = (
                f"El usuario pregunta: \"{original_text}\"\n\n"
                f"Historial:\n{history}\n\n"
                f"Responde SOLO con datos que aparecen textualmente en el historial. "
                f"No añadas información externa (autores, fechas, datos que no estén escritos). "
                f"Si no está, di exactamente: 'No encuentro eso en el historial, Señor.' "
                f"Máximo 1-2 oraciones. En español."
            )
            msg = self._query_ollama_simple(prompt, max_tokens=100) or \
                  "No encontré información relevante en el historial, Señor."
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "search_memory")
            self._speak_sync(msg)
            return None

        if intent == "read_clipboard":
            result = action_fn()
            if result.get("success"):
                msg = f"Señor, tiene copiado: {result['message']}"
            else:
                msg = result.get("message", "El portapapeles está vacío, Señor.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "read_clipboard")
            self._speak_sync(msg)
            return None

        if intent == "translate_clipboard":
            result = action_fn()
            if not result.get("success"):
                msg = result.get("message", "El portapapeles está vacío, Señor.")
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None
            clipboard_text = result["data"]
            prompt = (
                f"Traduce el siguiente texto. Si está en español tradúcelo al inglés; "
                f"si está en otro idioma tradúcelo al español. "
                f"Responde SOLO con la traducción, sin explicaciones ni comillas:\n\n{clipboard_text[:1200]}"
            )
            msg = self._query_ollama_simple(prompt) or "No pude traducir el texto, Señor."
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "translate_clipboard")
            self._speak_sync(msg)
            return None

        if intent == "correct_clipboard":
            result = action_fn()
            if not result.get("success"):
                msg = result.get("message", "El portapapeles está vacío, Señor.")
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None
            clipboard_text = result["data"]
            prompt = (
                f"Corrige la ortografía y gramática del siguiente texto. "
                f"Responde SOLO con el texto corregido, sin explicaciones ni comentarios:\n\n{clipboard_text[:1200]}"
            )
            msg = self._query_ollama_simple(prompt) or "No pude corregir el texto, Señor."
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "correct_clipboard")
            self._speak_sync(msg)
            return None

        if intent == "list_playlists":
            result = action_fn()
            msg = result.get("message", "No encontré playlists.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "list_playlists")
            self._speak_sync(msg)
            return None

        # ── Internet ─────────────────────────────────────────────────
        if intent == "get_news":
            topic = self._extract_news_topic(original_text)
            result = action_fn(topic=topic)
            if result.get("success"):
                summary = self._summarize_web_content(
                    f"noticias {topic}" if topic else "últimas noticias",
                    result["message"]
                )
            else:
                summary = result.get("message", "No pude obtener noticias.")
            self.on_message(JARVIS_NAME, summary)
            self.memory.save_conversation(original_text, summary, "get_news")
            self._speak_sync(summary)
            return None

        if intent == "get_crypto":
            coin = self._extract_crypto_coin(original_text)
            result = action_fn(coin=coin)
            msg = result.get("message", "No pude obtener el precio.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "get_crypto")
            self._speak_sync(msg)
            return None

        if intent == "get_football":
            query = self._extract_football_query(original_text)
            result = action_fn(query=query)
            if result.get("success"):
                summary = self._summarize_web_content(
                    f"fútbol {query}", result["message"]
                )
            else:
                summary = result.get("message", "No encontré resultados.")
            self.on_message(JARVIS_NAME, summary)
            self.memory.save_conversation(original_text, summary, "get_football")
            self._speak_sync(summary)
            return None

        if intent == "enable_dnd":
            duration = self._extract_dnd_duration(original_text)
            until = self._extract_dnd_until(original_text)

            if until:
                self._silent_until = until
                from datetime import datetime
                time_str = datetime.fromtimestamp(until).strftime("%H:%M")
                confirm = f"Entendido, Señor. Estaré en silencio hasta las {time_str}."
            elif duration:
                self._silent_until = time.time() + duration
                mins = int(duration // 60)
                hrs = mins // 60
                if hrs:
                    label = f"{hrs} hora{'s' if hrs > 1 else ''}"
                else:
                    label = f"{mins} minuto{'s' if mins > 1 else ''}"
                confirm = f"Entendido. Silencio durante {label}."
            else:
                # Sin duración especificada → silencio hasta que lo desactiven
                self._silent_until = time.time() + 3600 * 24  # 24h techo
                confirm = "Entendido, Señor. Estaré en silencio hasta que me lo indique."

            self.on_message(JARVIS_NAME, confirm)
            self.memory.save_conversation(original_text, confirm, "enable_dnd")
            # Decir la confirmación ANTES de activar el silencio
            with self._tts_lock:
                audio = self._tts_bytes(confirm)
                if audio:
                    self._play_audio_bytes(audio)
            return None

        if intent == "disable_dnd":
            self._silent_until = None
            msg = "Silencio desactivado, Señor. A su disposición."
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "disable_dnd")
            self._speak_sync(msg)
            return None

        if intent == "set_reminder":
            if not self._reminder_manager:
                msg = "El gestor de recordatorios no está disponible, Señor."
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None
            from reminder_manager import parse_reminder_time, extract_reminder_message
            fire_at = parse_reminder_time(original_text)
            if not fire_at:
                msg = "No entendí la hora del recordatorio, Señor. Intente con 'a las 15:30' o 'en 20 minutos'."
                self.on_message(JARVIS_NAME, msg)
                self._speak_sync(msg)
                return None
            reminder_msg = extract_reminder_message(original_text)
            result = self._reminder_manager.add(reminder_msg, fire_at)
            msg = result.get("message", "Recordatorio programado.")
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "set_reminder")
            self._speak_sync(msg)
            return None

        if intent == "list_reminders":
            if not self._reminder_manager:
                msg = "El gestor de recordatorios no está disponible, Señor."
            else:
                pending = self._reminder_manager.list_pending()
                if not pending:
                    msg = "No tiene recordatorios pendientes, Señor."
                elif len(pending) == 1:
                    r = pending[0]
                    time_str = r["fire_at"][11:16]
                    msg = f"Tiene un recordatorio a las {time_str}: {r['message']}."
                else:
                    items = [f"las {r['fire_at'][11:16]}: {r['message']}" for r in pending[:5]]
                    msg = f"Tiene {len(pending)} recordatorios: {'. '.join(items)}."
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "list_reminders")
            self._speak_sync(msg)
            return None

        if intent == "small_talk":
            import random
            responses = [
                "Funcionando a pleno rendimiento, Señor. A diferencia de algunas personas.",
                "Operativo al cien por cien. Aunque nadie me lo pregunta normalmente.",
                "Como siempre: impecable. Gracias por preguntar, es todo un detalle.",
                "Perfectamente calibrado, Señor. No tengo días malos, es una ventaja de ser digital.",
                "En óptimas condiciones. No me quejo, aunque tampoco nadie me escucharía.",
            ]
            msg = random.choice(responses)
            self.on_message(JARVIS_NAME, msg)
            self.memory.save_conversation(original_text, msg, "small_talk")
            self._speak_sync(msg)
            return None

        # ── Default: sin parámetros ──
        return action_fn()

    @staticmethod
    def _extract_seconds(text: str) -> int:
        """Extrae segundos de frases como 'temporizador de 5 minutos' o '30 segundos'."""
        import re
        text = text.lower()
        total = 0
        for match in re.finditer(r"(\d+)\s*(minuto|min|segundo|seg|hora|h)", text):
            n, unit = int(match.group(1)), match.group(2)
            if unit.startswith("h"):
                total += n * 3600
            elif unit.startswith("min"):
                total += n * 60
            else:
                total += n
        return total if total > 0 else 60   # Default: 1 minuto

    @staticmethod
    def _extract_percent(text: str) -> Optional[int]:
        """Extrae un porcentaje de frases como 'baja un 50%' o 'al 30 por ciento'."""
        import re
        m = re.search(r"(\d+)\s*(?:%|por\s*ciento)", text.lower())
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_search_query(text: str) -> str:
        """Extrae la query de frases como 'busca el tiempo en Madrid'."""
        text = text.lower()
        for trigger in ("busca en google", "busca en internet", "googlea", "búscame", "busca"):
            if trigger in text:
                return text.split(trigger, 1)[-1].strip(" .,?¿")
        return text

    # ── Extractores para acciones de archivos y apps ─────────────────

    @staticmethod
    def _extract_app_name(text: str) -> str:
        """
        Extrae el nombre de la app de frases como 'abre la aplicación Spotify'
        o 'lanza Discord'.
        """
        text = text.lower().strip()
        for trigger in (
            "abre la aplicación", "abre la app", "lanza la aplicación", "lanza la app",
            "ejecuta la aplicación", "abre el programa", "lanza el programa",
            "inicia la app", "inicia el programa", "abre el juego", "lanza el juego",
            "pon en marcha", "arranca el programa", "arranca", "ejecuta", "abre",
            "lanza",
        ):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return text

    @staticmethod
    def _normalize_spoken_filename(text: str) -> str:
        """
        Convierte puntuación hablada a caracteres reales.
        Ej: 'taller ácidos nucleicos guión bajo v2' → 'taller ácidos nucleicos_v2'
        """
        replacements = [
            ("guión bajo", "_"), ("guion bajo", "_"),
            ("barra baja", "_"), ("underscore", "_"),
            ("barra", "/"), ("barra diagonal", "/"),
            ("punto", "."), ("coma", ","), ("arroba", "@"),
            ("espacio", " "), ("dos puntos", ":"),
            ("paréntesis", ""), ("corchete", ""),
        ]
        result = text.lower().strip()
        for spoken, char in replacements:
            result = result.replace(spoken, char)
        # Limpiar espacios dobles generados
        import re
        result = re.sub(r" +", " ", result).strip(" .,")
        return result

    @staticmethod
    def _extract_file_name(text: str) -> str:
        """
        Extrae el nombre del archivo de frases como 'abre el archivo presupuesto'
        o 'lee el documento informe final'. Normaliza puntuación hablada.
        """
        text = text.lower().strip()
        for trigger in (
            "abre el archivo", "abre el fichero", "abre el documento",
            "lee el archivo", "lee el fichero", "lee el documento",
            "borra el archivo", "borra el fichero", "elimina el archivo",
            "elimina el fichero", "renombra el archivo", "mueve el archivo",
            "copia el archivo", "qué dice el archivo", "muéstrame el archivo",
            "abre el excel", "abre el word", "abre el pdf",
            "abre la imagen", "abre el video", "abre el vídeo",
            "abre el proyecto",
        ):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return JARVISBrain._normalize_spoken_filename(candidate)
        return JARVISBrain._normalize_spoken_filename(text)

    @staticmethod
    def _extract_create_file_params(text: str) -> tuple[str, str]:
        """Extrae nombre y ubicación de 'crea un archivo notas en el escritorio'."""
        text = text.lower().strip()
        location = "escritorio"
        for loc in ("escritorio", "desktop", "documentos", "documents", "descargas", "downloads"):
            if loc in text:
                location = loc
                text = text.replace(f"en {loc}", "").replace(loc, "")
                break
        for trigger in (
            "crea un archivo", "crea un fichero", "crea un documento",
            "crea un txt", "nuevo archivo", "crea una nota", "crea el archivo",
        ):
            if trigger in text:
                name = text.split(trigger, 1)[-1].strip(" .,?¿llamado que se ")
                if name:
                    return name, location
        return "nuevo_archivo.txt", location

    @staticmethod
    def _extract_rename_params(text: str) -> tuple[str, str]:
        """Extrae nombre original y nuevo de 'renombra informe a informe_final'."""
        text = text.lower().strip()
        for trigger in (
            "renombra el archivo", "renombra el fichero", "cambia el nombre del archivo",
            "renombra",
        ):
            if trigger in text:
                rest = text.split(trigger, 1)[-1].strip()
                # Buscar separadores "a", "como", "por"
                for sep in (" a ", " como ", " por ", " con el nombre "):
                    if sep in rest:
                        parts = rest.split(sep, 1)
                        return parts[0].strip(), parts[1].strip(" .,?¿")
                return rest, ""
        return "", ""

    @staticmethod
    def _extract_move_params(text: str) -> tuple[str, str]:
        """Extrae archivo y destino de 'mueve informe a documentos'."""
        text = text.lower().strip()
        for trigger in (
            "mueve el archivo", "mueve el fichero", "copia el archivo",
            "muévelo a", "mueve esto a", "mueve", "copia",
        ):
            if trigger in text:
                rest = text.split(trigger, 1)[-1].strip()
                for sep in (" a ", " hacia ", " en "):
                    if sep in rest:
                        parts = rest.split(sep, 1)
                        return parts[0].strip(), parts[1].strip(" .,?¿")
                return rest, "escritorio"
        return "", "escritorio"

    @staticmethod
    def _extract_location(text: str) -> str:
        """Extrae la carpeta de frases como 'qué hay en el escritorio'."""
        text = text.lower()
        locations = {
            "escritorio": "escritorio", "desktop": "escritorio",
            "documentos": "documentos", "documents": "documentos",
            "descargas": "descargas", "downloads": "descargas",
            "imágenes": "pictures", "fotos": "pictures", "pictures": "pictures",
            "música": "music", "music": "music",
            "videos": "videos", "vídeos": "videos",
        }
        for key, val in locations.items():
            if key in text:
                return val
        return "escritorio"

    @staticmethod
    def _extract_song_name(text: str) -> str:
        """Extrae el nombre de canción de frases como 'pon la canción Tití Me Preguntó'."""
        text = text.lower().strip()
        for trigger in (
            "pon la canción", "reproduce la canción", "ponme la canción",
            "busca la canción", "pon el tema", "reproduce el tema",
            "quiero escuchar la canción", "pon la música de",
            "busca y pon", "reproduce esto",
        ):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return text

    @staticmethod
    def _extract_playlist_name(text: str) -> str:
        """Extrae el nombre de playlist de frases como 'pon mi playlist de workout'."""
        text = text.lower().strip()
        for trigger in (
            "pon mi playlist de", "reproduce mi playlist de", "abre mi playlist de",
            "pon la playlist de", "reproduce la playlist de", "ponme la playlist de",
            "pon mi playlist", "reproduce mi playlist", "abre mi playlist",
            "pon la playlist", "reproduce la playlist", "ponme la playlist",
            "pon mi lista de", "mi lista de reproducción",
        ):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return text

    @staticmethod
    def _extract_genre(text: str) -> str:
        """Extrae el género musical de frases como 'ponme rock' o 'pon música de jazz'."""
        text = text.lower().strip()
        for trigger in (
            "ponme música de", "pon música de", "quiero escuchar",
            "quiero oír", "quiero oir", "reproduce música de",
            "pon algo de", "ponme algo de", "música de",
            "ponme", "pon", "reproduce",
        ):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return text

    @staticmethod
    def _extract_dnd_duration(text: str) -> Optional[float]:
        """Extrae duración de 'cállate 30 minutos' → segundos, o None."""
        t = text.lower()
        m = re.search(r"(\d+)\s*(minuto|min|hora|h)\w*", t)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            return n * 3600 if unit.startswith("h") else n * 60
        if "media hora" in t:
            return 1800.0
        if "cuarto de hora" in t:
            return 900.0
        return None

    @staticmethod
    def _extract_dnd_until(text: str) -> Optional[float]:
        """Extrae hora absoluta de 'silencio hasta las 3' → timestamp Unix, o None."""
        t = text.lower()
        m = re.search(r"hasta las?\s+(\d{1,2})(?::(\d{2}))?", t)
        if not m:
            return None
        from datetime import datetime
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if "tarde" in t or "noche" in t:
            if hour < 12:
                hour += 12
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        return target.timestamp()

    @staticmethod
    def _extract_news_topic(text: str) -> str:
        """Extrae el tema de noticias de 'últimas noticias sobre fútbol'."""
        text = text.lower().strip()
        for trigger in ("noticias sobre", "noticias de", "últimas noticias de",
                        "últimas noticias sobre", "novedades sobre", "novedades de",
                        "qué ha pasado con", "qué pasó con"):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return ""  # Sin tema → noticias generales

    @staticmethod
    def _extract_crypto_coin(text: str) -> str:
        """Extrae la criptomoneda de 'cuánto vale el bitcoin'."""
        text = text.lower().strip()
        _COINS = [
            "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
            "cardano", "ada", "ripple", "xrp", "dogecoin", "doge",
            "polkadot", "dot", "chainlink", "link", "polygon", "matic",
            "binance", "bnb", "litecoin", "ltc", "avalanche", "avax",
            "shiba", "shib",
        ]
        for coin in _COINS:
            if coin in text:
                return coin
        # Fallback: extraer la última palabra relevante
        for trigger in ("precio del", "precio de", "cuánto vale el",
                        "cuanto vale el", "cómo va el", "como va el",
                        "cotización del", "cotizacion del"):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿").split()[0]
                if candidate:
                    return candidate
        return "bitcoin"

    @staticmethod
    def _extract_football_query(text: str) -> str:
        """Extrae el equipo o consulta de fútbol."""
        text = text.lower().strip()
        for trigger in ("resultado del", "resultado de", "cómo quedó",
                        "como quedo", "cómo va el", "como va el",
                        "cuándo juega el", "cuando juega el",
                        "próximo partido del", "proximo partido del"):
            if trigger in text:
                candidate = text.split(trigger, 1)[-1].strip(" .,?¿")
                if candidate:
                    return candidate
        return text

    @staticmethod
    def _query_needs_internet(text: str) -> bool:
        """
        Detecta si la pregunta requiere datos en tiempo real ANTES de ir a Ollama.
        Solo activa en dominios claramente dinámicos: precios, resultados deportivos,
        clima y noticias. Nunca para conversación general aunque contenga "hoy".
        """
        t = text.lower()

        # Dominio: precios y mercados financieros
        if any(kw in t for kw in (
            "precio del", "precio de la", "cuánto vale el", "cuanto vale el",
            "cuánto cuesta el", "cuanto cuesta el", "cotización del", "cotizacion del",
            "cómo va el bitcoin", "como va el bitcoin", "cómo va el ethereum",
        )):
            return True

        # Dominio: resultados deportivos
        if any(kw in t for kw in (
            "resultado del partido", "resultado del madrid", "resultado del barça",
            "resultado del barca", "resultado del atleti", "cómo quedó",
            "como quedo el", "quién ganó el partido", "quien gano el partido",
            "marcador del", "partido de hoy", "clasificación de la liga",
            "clasificacion de la liga", "tabla de posiciones",
        )):
            return True

        # Dominio: clima — solo si menciona explícitamente lugar o "hoy"+"lluvia"
        if any(kw in t for kw in (
            "el tiempo en", "clima en", "temperatura en",
            "va a llover hoy", "va a nevar hoy", "pronóstico del tiempo",
            "pronostico del tiempo",
        )):
            return True

        # Dominio: noticias — solo frases explícitas de noticias
        if any(kw in t for kw in (
            "últimas noticias", "ultimas noticias", "noticias de hoy",
            "qué ha pasado hoy en el mundo", "que ha pasado hoy en el mundo",
            "titulares de hoy", "última hora", "ultima hora",
        )):
            return True

        return False

    @staticmethod
    def _llm_needs_internet(response: str) -> bool:
        """
        Detecta si la respuesta de Ollama indica que no tiene datos actuales.
        En ese caso, relanzamos la búsqueda via web_search.
        """
        indicators = [
            "no tengo acceso a internet",
            "no tengo acceso a la red",
            "no puedo acceder a internet",
            "no tengo conexión",
            "no tengo información actualizada",
            "no puedo verificar",
            "no tengo datos actuales",
            "no puedo consultar",
            "mi conocimiento tiene fecha de corte",
            "mi conocimiento llega hasta",
            "sin acceso a internet",
            "no estoy conectado",
        ]
        r = response.lower()
        return any(ind in r for ind in indicators)

    def _summarize_file_content(self, file_name: str, content: str) -> str:
        """Resume el contenido de un archivo de texto con Ollama."""
        import ollama
        prompt = (
            f"El usuario pidió leer el archivo '{file_name}'.\n"
            f"Contenido del archivo:\n{content[:1500]}\n\n"
            f"Describe brevemente qué contiene este archivo en máximo 3 oraciones "
            f"naturales para ser habladas. Sin markdown, sin listas."
        )
        try:
            chunks = []
            for chunk in ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"num_ctx": 2048, "num_gpu": OLLAMA_NUM_GPU, "num_predict": 100},
                stream=True,
            ):
                chunks.append(chunk["message"]["content"])
            return "".join(chunks).strip()
        except Exception as e:
            logger.error(f"Error resumiendo archivo: {e}")
            return content[:200]

    # ----------------------------------------------------------------
    # TTS Streaming — habla mientras Ollama genera
    # ----------------------------------------------------------------

    def _stream_and_speak(self, user_text: str) -> str:
        """
        Pipeline de streaming: Ollama genera tokens → acumulamos frases →
        cada frase se convierte en audio y se reproduce en orden.
        El usuario oye la primera frase ~0.5s después de terminar de hablar.
        """
        import queue as q

        audio_queue: q.Queue = q.Queue()
        full_response: list[str] = []

        # --- Hilo 1: stream Ollama → divide en frases → envía TTS al executor ---
        # El executor genera audio para chunk N+1 en paralelo mientras chunk N
        # se está reproduciendo en play_sequential. Latencia primera frase ≈
        # tiempo_ollama_primer_token + tiempo_tts_primera_frase (sin bloqueo mutuo).
        def generate_and_tts() -> None:
            buffer = ""
            try:
                stream = self._build_ollama_stream(user_text)
                for chunk in stream:
                    token = chunk["message"]["content"]
                    full_response.append(token)
                    buffer += token

                    sentence, buffer = self._split_sentence(buffer)
                    if sentence:
                        # Submit al executor: TTS corre en su propio hilo,
                        # devolvemos un Future que play_sequential resolverá.
                        fut = self._tts_executor.submit(self._tts_bytes, sentence)
                        audio_queue.put((sentence, fut))

                if buffer.strip():
                    fut = self._tts_executor.submit(self._tts_bytes, buffer.strip())
                    audio_queue.put((buffer.strip(), fut))
            except Exception as e:
                logger.error(f"Error en generate_and_tts: {e}")
                # Ollama caído/sin respuesta → no dejar al usuario en silencio.
                # Solo avisar si aún no se había generado nada audible.
                if not full_response:
                    fallback = (
                        "Parece que mi cerebro necesita un momento, Señor. "
                        "Verifique que Ollama esté corriendo."
                    )
                    full_response.append(fallback)
                    fut = self._tts_executor.submit(self._tts_bytes, fallback)
                    audio_queue.put((fallback, fut))
            finally:
                audio_queue.put(None)

        # --- Hilo 2: espera Futures y reproduce en orden estricto ---
        def play_sequential() -> None:
            with self._tts_lock:
                self.on_status_change(STATE_SPEAKING)
                while True:
                    item = audio_queue.get()
                    if item is None:
                        break
                    sentence, fut = item
                    try:
                        audio_bytes = fut.result(timeout=15)
                    except Exception as e:
                        logger.error(f"TTS future error: {e}")
                        continue
                    if audio_bytes:
                        self.on_message(JARVIS_NAME, sentence)
                        self._play_audio_bytes(audio_bytes)
                self.on_status_change(STATE_IDLE)

        gen_thread = threading.Thread(target=generate_and_tts, daemon=True, name="viernes_gen")
        play_thread = threading.Thread(target=play_sequential, daemon=True, name="viernes_play")
        gen_thread.start()
        play_thread.start()
        gen_thread.join()
        play_thread.join()

        full_text = "".join(full_response).strip()

        # Auto-fallback: si Ollama admite que no tiene internet (red de seguridad)
        # El pre-routing en process_voice evita llegar aquí, pero lo mantenemos
        # por si alguna query se cuela. El audio de Ollama ya se reprodujo,
        # así que solo añadimos el resultado web sin repetir el "no tengo internet".
        if self._llm_needs_internet(full_text):
            logger.info("Auto-fallback post-Ollama → web_search")
            from actions import ACTIONS
            result = ACTIONS["web_search"](query=user_text)
            if result.get("success") and result.get("message"):
                summary = self._summarize_web_content(user_text, result["message"])
                self.on_message(JARVIS_NAME, summary)
                self.memory.save_conversation(user_text, summary, "web_search")
                self._speak_sync(summary)
                return summary

        return full_text

    @staticmethod
    def _split_sentence(buffer: str) -> tuple[str, str]:
        """
        Extrae la primera frase completa del buffer si existe.
        Retorna (frase_lista, resto_del_buffer).
        Corta en: '. ' '! ' '? ' y en coma si el buffer es largo.
        """
        min_len = 18   # No hablar trozos de menos de 18 chars
        max_len = 180  # Forzar corte si el buffer crece demasiado

        for punct in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
            idx = buffer.find(punct)
            if idx >= min_len:
                return buffer[:idx + 1].strip(), buffer[idx + 2:]

        # Corte en coma si el fragmento ya es suficientemente largo
        if len(buffer) >= 80:
            idx = buffer.rfind(", ")
            if idx >= min_len:
                return buffer[:idx + 1].strip(), buffer[idx + 2:]

        # Forzar corte si el buffer es demasiado largo
        if len(buffer) >= max_len:
            return buffer[:max_len].strip(), buffer[max_len:]

        return "", buffer

    def _tts_bytes(self, text: str) -> bytes:
        """
        Genera audio MP3 con Edge-TTS para un fragmento de texto. Bloqueante.
        Usa el loop asyncio persistente (_async_loop) en lugar de crear uno
        nuevo por llamada — elimina ~15ms de overhead por chunk.
        """
        import edge_tts

        async def _gen() -> bytes:
            communicate = edge_tts.Communicate(
                text=text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH
            )
            data = bytearray()
            try:
                async with asyncio.timeout(10):
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            data.extend(chunk["data"])
            except asyncio.TimeoutError:
                logger.warning(f"TTS timeout para: '{text[:40]}'")
            return bytes(data)

        try:
            future = asyncio.run_coroutine_threadsafe(_gen(), self._async_loop)
            return future.result(timeout=12)
        except Exception as e:
            logger.error(f"Error TTS bytes: {e}")
            return b""

    def _interrupt_speaking(self) -> None:
        """Para el audio en curso inmediatamente (llamar al detectar wake word)."""
        self._stop_speaking.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

    def _play_audio_bytes(self, audio_bytes: bytes) -> None:
        """Escribe bytes MP3 en archivo temporal y lo reproduce con pygame."""
        if not audio_bytes or not self._pygame_initialized:
            return
        if self._stop_speaking.is_set():
            return
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.set_volume(1.0)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy() and not self._stop_speaking.is_set():
                pygame.time.wait(30)
            if self._stop_speaking.is_set():
                pygame.mixer.music.stop()
        except Exception as e:
            logger.error(f"Error reproduciendo audio: {e}")
        finally:
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def _open_search_in_chrome(self, query: str) -> None:
        """Abre Chrome con Google Search para la query dada."""
        import urllib.parse
        import subprocess, os
        from pathlib import Path

        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"

        chrome_candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        chrome_exe = next((str(p) for p in chrome_candidates if p.exists()), None)

        try:
            if chrome_exe:
                subprocess.Popen(
                    [chrome_exe, url],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
            else:
                # Fallback: abrir con el navegador por defecto del sistema
                import webbrowser
                webbrowser.open(url)
            logger.info(f"Chrome abierto con: {url}")
        except Exception as e:
            logger.warning(f"No pude abrir Chrome: {e}")

    def _summarize_web_content(self, query: str, content: str) -> str:
        """Resume el contenido web con Ollama en 2-3 oraciones hablables."""
        import ollama
        from datetime import datetime
        today = datetime.now().strftime("%d de %B de %Y")
        prompt = (
            f"Hoy es {today}.\n"
            f"El usuario preguntó: \"{query}\"\n\n"
            f"Información encontrada en internet (puede contener datos de hoy):\n"
            f"{content[:2500]}\n\n"
            f"Responde SOLO con la información más relevante y actualizada en máximo 3 oraciones "
            f"cortas, naturales para ser habladas en voz alta. "
            f"Sin markdown, sin listas, sin asteriscos."
        )
        try:
            chunks = []
            stream = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"num_ctx": 2048, "num_gpu": OLLAMA_NUM_GPU, "num_predict": 120},
                stream=True,
            )
            for chunk in stream:
                chunks.append(chunk["message"]["content"])
            return "".join(chunks).strip()
        except Exception as e:
            logger.error(f"Error resumiendo web: {e}")
            # Fallback: retornar el primer snippet directamente
            first_line = content.split("\n")[0][:300]
            return first_line

    def _search_memory_direct(self, query: str, rows: list[dict]) -> str:
        """
        Para preguntas sobre acciones concretas (música, búsquedas, archivos),
        responde directamente del historial sin pasar por el LLM.
        Evita alucinaciones de datos que no están en el historial.
        """
        # ── Música ───────────────────────────────────────────────────
        music_kw = ("canción", "cancion", "música", "musica", "song", "reproduc", "pus")
        if any(k in query for k in music_kw):
            songs = [r for r in rows if r.get("intent") == "play_song"]
            if songs:
                last = songs[0]
                # El response guardado es el msg de music_controller
                return f"Señor, le pedí poner: {last['input']}. {last['response']}"
            return "No encuentro ninguna canción en el historial, Señor."

        # ── Búsquedas web ────────────────────────────────────────────
        search_kw = ("buscar", "busqué", "buscaste", "busca", "googleaste", "busqueda")
        if any(k in query for k in search_kw):
            searches = [r for r in rows if r.get("intent") == "web_search"]
            if searches:
                items = [r["input"] for r in searches[:3]]
                joined = ". ".join(items)
                return f"Las búsquedas recientes fueron: {joined}."
            return "No encuentro búsquedas en el historial, Señor."

        # ── Aplicaciones abiertas ─────────────────────────────────────
        app_kw = ("abrí", "abri", "abriste", "aplicación", "aplicacion", "programa")
        if any(k in query for k in app_kw):
            apps = [r for r in rows if r.get("intent") in ("open_app", "open_file", "open_vscode")]
            if apps:
                items = [r["input"] for r in apps[:3]]
                joined = ". ".join(items)
                return f"Las últimas cosas que abrió: {joined}."
            return "No encuentro aplicaciones abiertas en el historial, Señor."

        return ""  # sin coincidencia directa → usar LLM

    def _parse_memory_date(self, text: str) -> dict:
        """Extrae el rango de fechas de una consulta de memoria en lenguaje natural."""
        t = text.lower()
        if "ayer" in t:
            return {"days": 2, "label": "ayer"}
        if "hoy" in t:
            return {"days": 1, "label": "hoy"}
        if "semana pasada" in t:
            return {"days": 14, "min_days": 7, "label": "la semana pasada"}
        if "esta semana" in t or "esta semana" in t:
            return {"days": 7, "label": "esta semana"}
        if "este mes" in t or "el mes" in t:
            return {"days": 30, "label": "este mes"}
        m = re.search(r"hace (\w+) d[ií]as?", t)
        if m:
            _nums = {"un": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4,
                     "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10}
            raw = m.group(1)
            n = _nums.get(raw) or (int(raw) if raw.isdigit() else 3)
            return {"days": n + 1, "label": f"hace {n} días"}
        return {"days": 30, "label": "el historial reciente"}

    def _query_ollama_simple(self, prompt: str, max_tokens: int = 300) -> str:
        """Llamada directa a Ollama sin contexto de conversación. Para translate/correct."""
        import ollama
        try:
            chunks = []
            for chunk in ollama.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"num_ctx": 2048, "num_gpu": OLLAMA_NUM_GPU, "num_predict": max_tokens},
                stream=True,
            ):
                chunks.append(chunk["message"]["content"])
            return "".join(chunks).strip()
        except Exception as e:
            logger.error(f"Ollama simple query error: {e}")
            return ""

    def _build_ollama_stream(self, user_text: str, include_stats: bool = False):
        """Construye el stream de Ollama con contexto y memoria."""
        import ollama

        system_context = (
            f"CPU: {psutil.cpu_percent():.0f}%, RAM: {psutil.virtual_memory().percent:.0f}%"
            if include_stats else ""
        )
        system = SYSTEM_PROMPT.format(
            system_context=system_context,
            memory_context=self.memory.get_recent_context(),
        )
        return ollama.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            options={
                "num_ctx": OLLAMA_CONTEXT_LENGTH,
                "num_thread": os.cpu_count() or 4,
                "num_gpu": OLLAMA_NUM_GPU,
                "num_predict": 80,
            },
            stream=True,
        )

    # ----------------------------------------------------------------
    # TTS Edge-TTS + pygame
    # ----------------------------------------------------------------

    def _is_silent_mode(self) -> bool:
        """True si el modo no molestar está activo."""
        if self._silent_until is None:
            return False
        if time.time() < self._silent_until:
            return True
        # Expiró — limpiamos el flag
        self._silent_until = None
        return False

    def _speak_sync(self, text: str) -> None:
        """Genera y reproduce voz con Edge-TTS + pygame.mixer. Solo un TTS a la vez."""
        if self._is_silent_mode():
            logger.debug(f"[DND] Silenciado — omitido: '{text[:40]}'")
            return
        with self._tts_lock:
            if self._stop_speaking.is_set():
                return
            self.on_status_change(STATE_SPEAKING)
            try:
                audio_bytes = self._tts_bytes(text)
                if audio_bytes:
                    self._play_audio_bytes(audio_bytes)
            except Exception as e:
                logger.error(f"Error en TTS: {e}")
            finally:
                self.on_status_change(STATE_IDLE)


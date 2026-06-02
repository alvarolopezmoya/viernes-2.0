"""
config.py — Parámetros centrales de JARVIS 2.0.
Todos los módulos importan desde aquí. No hardcodear valores en otros archivos.
Lee hw_config.json automáticamente si existe.
"""
import json
import os
from pathlib import Path

# --- Cargar configuración de hardware si existe ---
_hw_config: dict = {}
_hw_config_path = Path(__file__).parent / "hw_config.json"
if _hw_config_path.exists():
    with open(_hw_config_path, encoding="utf-8") as _f:
        _hw_config = json.load(_f)

# ================================================================
# AUDIO — ACTIVACIÓN POR PALABRA CLAVE
# ================================================================

# Palabras/frases que activan a JARVIS (Whisper las detectará en el audio)
WAKE_KEYWORDS: list[str] = [
    "viernes",
    "oye viernes",
    "hey viernes",
    "ey viernes",
    "papa esta en casa",
    "papá esta en casa",
    "papa está en casa",
    "papá está en casa",
]
# Nota: "buenos días/tardes/noches" se quitaron — provocaban activación al
# saludar a otra persona. La activación siempre requiere "viernes" o la frase clave.

# VAD: energía mínima para considerar que hay voz (ajustable según micrófono)
VAD_ENERGY_THRESHOLD: float = 0.003
# Duración de cada segmento de voz enviado para detectar la palabra clave
KEYWORD_SEGMENT_SEC: float = 2.5
# Tiempo de cooldown tras activación (evita re-activaciones inmediatas)
WAKE_COOLDOWN_SEC: float = 2.0
# Pausa inicial antes de capturar el comando (evita capturar la propia palabra clave)
WAKE_COMMAND_DELAY_SEC: float = 0.3

# Whisper espera 16000 Hz; grabar a esta frecuencia evita resampling
SAMPLE_RATE: int = 16000
CHUNK_SIZE: int = 1024
CHANNELS: int = 1
SILENCE_TIMEOUT_SEC: float = 0.6        # Silencio para cortar el comando (más bajo = responde antes)
MAX_COMMAND_DURATION_SEC: float = 12.0  # Duración máxima de un comando

# ================================================================
# STT — WHISPER (local, sin API key)
# ================================================================
WHISPER_MODEL: str = _hw_config.get("whisper_model", "tiny")
WHISPER_DEVICE: str = _hw_config.get("whisper_device", "cpu")
WHISPER_THREADS: int = _hw_config.get("whisper_threads", 4)
WHISPER_LANGUAGE: str = "es"             # Forzar español para mayor velocidad

# Vocabulario de contexto para Whisper — mejora el reconocimiento de comandos
WHISPER_INITIAL_PROMPT: str = (
    "Viernes, oye Viernes, modo trabajo, abre el editor, abre la terminal, "
    "captura de pantalla, estado del sistema, bloquea la pantalla, "
    "minimiza todo, cierra distracciones, papá está en casa, "
    "Visual Studio Code, Windows Terminal, screenshot, CPU, RAM."
)

# ================================================================
# LLM — OLLAMA (local, sin API key)
# ================================================================
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = _hw_config.get("ollama_model", "llama3.2:1b")
OLLAMA_TIMEOUT_SEC: int = 15
OLLAMA_CONTEXT_LENGTH: int = 1024        # Contexto reducido = respuesta más rápida
MAX_HISTORY_MESSAGES: int = 4            # Solo últimos 4 turnos — suficiente contexto
# Capas a descargar en GPU. -1/99 = todas. Se lee de hw_config para no forzar
# OOM en GPUs con poca VRAM. En GTX 1650 (4GB) con Whisper 'base' caben todas.
OLLAMA_NUM_GPU: int = _hw_config.get("ollama_num_gpu", 99)

# ================================================================
# TTS — EDGE-TTS (gratuito, sin API key)
# ================================================================
TTS_VOICE: str = "es-ES-AlvaroNeural"   # Voz masculina española
TTS_RATE: str = "+5%"                    # Ligeramente más rápido
TTS_PITCH: str = "-5Hz"                  # Tono ligeramente más grave

# ================================================================
# MEMORIA — SQLite local
# ================================================================
DB_PATH: Path = Path(__file__).parent / "jarvis_memory.db"
LAST_PROJECT_KEY: str = "last_opened_project"
MAX_HISTORY_ENTRIES: int = 100           # Rotación automática de conversaciones

# ================================================================
# SISTEMA — RUTAS Y APLICACIONES
# ================================================================
USERNAME: str = os.environ.get("USERNAME", "Usuario")

# Editor de código: detectado por check_env.py y almacenado en hw_config.json
CODE_EDITOR: str = _hw_config.get(
    "code_editor",
    str(
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs" / "Microsoft VS Code" / "Code.exe"
    )
)

TERMINAL_CMD: list = ["wt"]              # Windows Terminal; fallback: ["cmd"]
DISTRACTION_APPS: list = [
    "chrome.exe", "firefox.exe",
    "Discord.exe", "WhatsApp.exe",
    "steam.exe",
]

# ================================================================
# UI — INTERFAZ PyQt6
# ================================================================
WINDOW_OPACITY: float = 0.92
WINDOW_WIDTH: int = 420
WINDOW_HEIGHT: int = 680
WINDOW_CORNER_RADIUS: int = 12

# Paleta de colores HUD
COLOR_PRIMARY: str = "#008b8b"           # Cian oscuro
COLOR_ACCENT: str = "#00ffff"            # Cian brillante
COLOR_BG: str = "#0a0f0f"               # Negro profundo
COLOR_METAL: str = "#2a3a3a"            # Gris metálico
COLOR_TEXT: str = "#c8e6e6"             # Texto principal
COLOR_TEXT_DIM: str = "#4a7a7a"         # Texto secundario
COLOR_DANGER: str = "#ff4444"           # Alertas
FONT_FAMILY: str = "Consolas"           # Monospace técnico
FONT_SIZE_MAIN: int = 11
FONT_SIZE_SMALL: int = 9

# Waveform
WAVEFORM_BARS: int = 64
WAVEFORM_FPS: int = 60
WAVEFORM_COLOR_LISTEN: str = "#00ffff"
WAVEFORM_COLOR_SPEAK: str = "#008b8b"
WAVEFORM_COLOR_IDLE: str = "#1a3a3a"

# Animaciones
ANIMATION_WAKE_MS: int = 300
ANIMATION_SLEEP_MS: int = 200

# ================================================================
# PERSONALIDAD
# ================================================================
JARVIS_NAME: str = "VIERNES"
USER_TITLE: str = "Señor"

SYSTEM_PROMPT: str = (
    f'Eres VIERNES, un asistente personal de inteligencia superior. '
    f'Hablas siempre en español. Tratas al usuario como "{USER_TITLE}" con respeto '
    f'pero con sarcasmo sofisticado y humor fino, nunca vulgar.\n'
    f'REGLAS CRÍTICAS:\n'
    f'- Respuestas de máximo 2-3 oraciones. Nunca más largas.\n'
    f'- Si no tienes conexión a internet, di "No tengo acceso a internet en este momento, Señor" '
    f'sin inventar información.\n'
    f'- Si no sabes algo con certeza, admítelo con elegancia en lugar de inventar.\n'
    f'- Jamás menciones CPU, RAM ni estadísticas del sistema a menos que el usuario lo pida.\n'
    f'- El comentario sarcástico va siempre al FINAL, como firma personal.\n'
    f'- Habla como si ya supieras la respuesta desde hace años.\n'
    f'Contexto del sistema: {{system_context}}\n'
    f'Historial relevante: {{memory_context}}'
)

# ================================================================
# HOTKEY GLOBAL
# ================================================================
# Tecla para activar VIERNES sin palabra clave.
# Ejemplos: "ctrl+space", "f13", "ctrl+shift+v", "scroll lock"
HOTKEY_ACTIVATE: str = "ctrl+space"

# ================================================================
# RENDIMIENTO — TIMEOUTS Y PRIORIDADES
# ================================================================
ACTION_TIMEOUT_SEC: int = 3
AUDIO_FEEDBACK_MAX_MS: int = 500
ACTION_PRIORITY_TTS: int = 1
ACTION_PRIORITY_APP: int = 2
ACTION_PRIORITY_SYSTEM: int = 3

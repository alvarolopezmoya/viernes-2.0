"""
actions.py — Acciones de automatización del sistema para JARVIS 2.0.

Todas las acciones son no-bloqueantes usando subprocess.Popen (fire-and-forget).
Cada función retorna: {"success": bool, "message": str, "data": Any}
Timeout máximo por acción: ACTION_TIMEOUT_SEC segundos.
"""
import os
import sqlite3
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Any

import psutil
import pyautogui

from config import (
    CODE_EDITOR,
    TERMINAL_CMD,
    DISTRACTION_APPS,
    ACTION_TIMEOUT_SEC,
    LAST_PROJECT_KEY,
    DB_PATH,
)

logger = logging.getLogger("jarvis.actions")

# Cache de warmup — pre-valida que las funciones existen al inicio
_PATH_CACHE: dict[str, bool] = {}


# ----------------------------------------------------------------
# Helpers internos
# ----------------------------------------------------------------

def _run_detached(cmd: list[str]) -> dict[str, Any]:
    """
    Ejecuta un proceso desacoplado de JARVIS (fire-and-forget).
    No bloquea el hilo llamante. Retorna inmediatamente.
    """
    try:
        subprocess.Popen(
            cmd,
            creationflags=(
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
            close_fds=True,
        )
        return {"success": True, "message": "Proceso iniciado.", "data": None}
    except FileNotFoundError:
        msg = f"Ejecutable no encontrado: {cmd[0]}"
        logger.error(msg)
        return {"success": False, "message": msg, "data": None}
    except Exception as e:
        logger.error(f"Error lanzando proceso {cmd}: {e}")
        return {"success": False, "message": str(e), "data": None}


def _get_last_project() -> str:
    """Lee la ruta del último proyecto abierto desde la base de datos."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA busy_timeout=3000")
        row = conn.execute(
            "SELECT value FROM preferences WHERE key=?", (LAST_PROJECT_KEY,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


def _set_last_project(path: str) -> None:
    """Guarda la ruta del último proyecto en la base de datos."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute(
            "INSERT OR REPLACE INTO preferences VALUES (?,?,datetime('now'))",
            (LAST_PROJECT_KEY, path),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"No se pudo guardar último proyecto: {e}")


# ----------------------------------------------------------------
# Acciones del sistema
# ----------------------------------------------------------------

def get_system_stats() -> dict[str, Any]:
    """Retorna métricas actuales de CPU, RAM y disco."""
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    # En Windows "/" se mapea a C:\; usar sys_drive como fallback explícito
    import sys
    sys_drive = os.environ.get("SYSTEMDRIVE", "C:\\") or "C:\\"
    try:
        disk = psutil.disk_usage(sys_drive)
    except Exception:
        disk = psutil.disk_usage("/")
    data = {
        "cpu_percent": cpu,
        "ram_percent": ram.percent,
        "ram_used_gb": round(ram.used / 1e9, 1),
        "ram_total_gb": round(ram.total / 1e9, 1),
        "disk_percent": disk.percent,
    }
    msg = (
        f"CPU al {cpu:.0f}%, "
        f"RAM al {ram.percent:.0f}% "
        f"({data['ram_used_gb']:.1f}/{data['ram_total_gb']:.1f} GB), "
        f"Disco al {disk.percent:.0f}%."
    )
    return {"success": True, "message": msg, "data": data}


def open_vscode() -> dict[str, Any]:
    """
    Abre el editor de código configurado (VS Code, Cursor o fallback).
    Si hay un último proyecto conocido, lo abre directamente.
    Advierte si no hay un editor real configurado.
    """
    editor_path = Path(CODE_EDITOR)
    # Detectar si el "editor" configurado es solo notepad (sin VS Code instalado)
    if editor_path.name.lower() in ("notepad.exe", "notepad"):
        logger.warning("No hay editor de código real configurado. Instale VS Code.")
        return {
            "success": False,
            "message": (
                "No hay editor de código instalado, Señor. "
                "Instale VS Code desde code.visualstudio.com."
            ),
            "data": None,
        }

    last_project = _get_last_project()
    cmd = [CODE_EDITOR]

    if last_project and Path(last_project).exists():
        cmd.append(last_project)
        logger.info(f"Abriendo editor con proyecto: {last_project}")
    else:
        logger.info(f"Abriendo editor sin proyecto: {CODE_EDITOR}")

    return _run_detached(cmd)


def open_terminal() -> dict[str, Any]:
    """Abre Windows Terminal o cmd como fallback."""
    result = _run_detached(TERMINAL_CMD)
    if not result["success"] and TERMINAL_CMD[0] == "wt":
        logger.warning("wt no disponible, intentando cmd.exe")
        result = _run_detached(["cmd.exe"])
    return result


def close_distractions() -> dict[str, Any]:
    """Termina todos los procesos de distracción configurados en DISTRACTION_APPS."""
    closed: list[str] = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"] in DISTRACTION_APPS:
                proc.terminate()
                closed.append(proc.info["name"])
                logger.info(f"Proceso terminado: {proc.info['name']}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as e:
            logger.warning(f"Error cerrando {proc.info.get('name', '?')}: {e}")

    unique_closed = list(dict.fromkeys(closed))   # Orden preservado, sin duplicados
    if unique_closed:
        msg = f"Cerrados: {', '.join(unique_closed)}."
    else:
        msg = "No había aplicaciones de distracción abiertas."
    return {"success": True, "message": msg, "data": unique_closed}


def activate_work_mode() -> dict[str, Any]:
    """
    Modo trabajo completo: cierra distracciones, abre editor y terminal.
    Ejecuta las acciones en secuencia con mínimo delay.
    """
    results: list[str] = []

    # 1. Cerrar distracciones
    r1 = close_distractions()
    results.append(r1["message"])

    # 2. Abrir editor
    r2 = open_vscode()
    results.append("Editor lanzado." if r2["success"] else r2["message"])

    # 3. Abrir terminal
    r3 = open_terminal()
    results.append("Terminal abierta." if r3["success"] else r3["message"])

    return {
        "success": True,
        "message": "Modo trabajo activado. " + " ".join(results),
        "data": results,
    }


def lock_screen() -> dict[str, Any]:
    """Bloquea la pantalla de Windows."""
    try:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return {"success": True, "message": "Pantalla bloqueada.", "data": None}
    except Exception as e:
        logger.error(f"Error bloqueando pantalla: {e}")
        return {"success": False, "message": str(e), "data": None}


def minimize_all() -> dict[str, Any]:
    """Minimiza todas las ventanas abiertas (Win+D)."""
    try:
        pyautogui.hotkey("win", "d")
        return {"success": True, "message": "Ventanas minimizadas.", "data": None}
    except Exception as e:
        logger.error(f"Error minimizando ventanas: {e}")
        return {"success": False, "message": str(e), "data": None}


def screenshot() -> dict[str, Any]:
    """Captura la pantalla completa y la guarda en ~/Pictures."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pictures = Path.home() / "Pictures"
        pictures.mkdir(parents=True, exist_ok=True)
        path = pictures / f"jarvis_capture_{timestamp}.png"
        img = pyautogui.screenshot()
        img.save(str(path))
        logger.info(f"Screenshot guardado: {path}")
        return {"success": True, "message": f"Guardado en {path}", "data": str(path)}
    except Exception as e:
        logger.error(f"Error en screenshot: {e}")
        return {"success": False, "message": str(e), "data": None}


def analyze_screen() -> dict[str, Any]:
    """Placeholder para módulo de visión futuro (Moondream2 u otro modelo local)."""
    msg = "Módulo de visión no implementado aún, Señor. Próximamente."
    return {"success": False, "message": msg, "data": None}


# ----------------------------------------------------------------
# Volumen del sistema (pycaw — control preciso en %)
# ----------------------------------------------------------------

def _get_volume_interface():
    """Retorna la interfaz de volumen de Windows vía pycaw."""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_current_volume() -> int:
    """Retorna el volumen actual del sistema (0-100)."""
    try:
        vol = _get_volume_interface()
        return round(vol.GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return -1


def volume_up(amount: int = 20) -> dict[str, Any]:
    """Sube el volumen del sistema en amount%."""
    try:
        vol = _get_volume_interface()
        current = vol.GetMasterVolumeLevelScalar()
        new_vol = min(1.0, current + amount / 100)
        vol.SetMasterVolumeLevelScalar(new_vol, None)
        return {"success": True, "message": f"Volumen al {round(new_vol*100)}%.", "data": round(new_vol*100)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def volume_down(amount: int = 20) -> dict[str, Any]:
    """Baja el volumen del sistema en amount%."""
    try:
        vol = _get_volume_interface()
        current = vol.GetMasterVolumeLevelScalar()
        new_vol = max(0.0, current - amount / 100)
        vol.SetMasterVolumeLevelScalar(new_vol, None)
        return {"success": True, "message": f"Volumen al {round(new_vol*100)}%.", "data": round(new_vol*100)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def volume_set(percent: int) -> dict[str, Any]:
    """Pone el volumen del sistema a un porcentaje exacto."""
    try:
        vol = _get_volume_interface()
        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100)), None)
        return {"success": True, "message": f"Volumen puesto al {percent}%.", "data": percent}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def mute_volume() -> dict[str, Any]:
    """Silencia o reactiva el audio del sistema."""
    try:
        vol = _get_volume_interface()
        muted = vol.GetMute()
        vol.SetMute(not muted, None)
        state = "silenciado" if not muted else "reactivado"
        return {"success": True, "message": f"Audio {state}.", "data": not muted}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def play_pause_media() -> dict[str, Any]:
    """Pausa o reanuda la reproducción de media (Spotify, YouTube, etc.)."""
    try:
        pyautogui.press("playpause")
        return {"success": True, "message": "Reproducción pausada/reanudada.", "data": None}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


# ----------------------------------------------------------------
# Aplicaciones
# ----------------------------------------------------------------

def open_chrome() -> dict[str, Any]:
    """Abre Google Chrome."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for p in candidates:
        if p.exists():
            return _run_detached([str(p)])
    # Fallback: usar el comando start de Windows
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "chrome"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return {"success": True, "message": "Chrome abierto.", "data": None}
    except Exception as e:
        return {"success": False, "message": f"Chrome no encontrado: {e}", "data": None}


def open_apple_music() -> dict[str, Any]:
    """Abre Apple Music."""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/AppleMusic.exe",
        Path("C:/Program Files/Apple Music/Apple Music.exe"),
    ]
    for p in candidates:
        if p.exists():
            return _run_detached([str(p)])
    return {"success": False, "message": "Apple Music no encontrado, Señor.", "data": None}


def web_search(query: str = "") -> dict[str, Any]:
    """
    Busca información actualizada:
    - Clima/tiempo → wttr.in (siempre en tiempo real)
    - Resto → DuckDuckGo con resultados del último día + scraping del primer resultado
    """
    if not query:
        return {"success": False, "message": "No se especificó qué buscar.", "data": None}

    import requests
    from bs4 import BeautifulSoup

    query_lower = query.lower()
    results_text: list[str] = []
    first_url = ""

    # ── Ruta 1: Clima/tiempo → wttr.in (datos en tiempo real) ──────────
    weather_words = ("tiempo", "clima", "temperatura", "lluvia", "nieve",
                     "calor", "frio", "frío", "weather", "pronóstico", "pronostico")
    if any(w in query_lower for w in weather_words):
        # Extraer ciudad de la query
        city = query_lower
        for w in ("el tiempo en", "clima en", "temperatura en", "tiempo en",
                  "qué tiempo hace en", "que tiempo hace en", "pronóstico en"):
            city = city.replace(w, "").strip()
        city = city.strip(" .,?¿") or "Madrid"

        try:
            resp = requests.get(
                f"https://wttr.in/{requests.utils.quote(city)}?format=j1",
                timeout=6,
                headers={"User-Agent": "curl/7.0"},
            )
            data = resp.json()
            current = data["current_condition"][0]
            desc    = current["weatherDesc"][0]["value"]
            temp_c  = current["temp_C"]
            feels   = current["FeelsLikeC"]
            humidity= current["humidity"]
            wind    = current["windspeedKmph"]

            today   = data["weather"][0]
            max_t   = today["maxtempC"]
            min_t   = today["mintempC"]

            summary = (
                f"En {city.title()} ahora mismo: {desc}, {temp_c}°C "
                f"(sensación {feels}°C). Humedad {humidity}%, viento {wind} km/h. "
                f"Hoy máxima {max_t}°C y mínima {min_t}°C."
            )
            return {"success": True, "message": summary,
                    "data": {"query": query, "url": f"wttr.in/{city}", "realtime": True}}
        except Exception as e:
            logger.warning(f"wttr.in falló ({e}), usando DuckDuckGo.")

    # ── Ruta 2: DuckDuckGo con resultados recientes ─────────────────────
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            # timelimit="d" → solo resultados de las últimas 24h
            hits = list(ddgs.text(query, region="es-es", max_results=5,
                                  safesearch="off", timelimit="d"))

        # Si no hay resultados de hoy, ampliar a la última semana
        if not hits:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, region="es-es", max_results=5,
                                      safesearch="off", timelimit="w"))

        if not hits:
            return {"success": False,
                    "message": "No encontré resultados actualizados.", "data": None}

        for h in hits[:3]:
            title = h.get("title", "")
            body  = h.get("body", "")
            if body:
                results_text.append(f"{title}: {body}")

        # Scraping del primer resultado para más detalle
        first_url = hits[0].get("href", "")
        if first_url:
            try:
                resp = requests.get(
                    first_url, timeout=5,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                soup = BeautifulSoup(resp.text, "lxml")
                for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                    tag.decompose()
                paragraphs = [
                    p.get_text(" ", strip=True)
                    for p in soup.find_all("p")
                    if len(p.get_text(strip=True)) > 60
                ]
                page_text = " ".join(paragraphs[:8])
                if page_text:
                    results_text.append(f"Fuente ({first_url}):\n{page_text}")
            except Exception:
                pass

        combined = "\n\n".join(results_text)[:3000]
        return {"success": True, "message": combined,
                "data": {"query": query, "url": first_url, "realtime": False}}

    except Exception as e:
        logger.error(f"Error en web_search: {e}")
        return {"success": False, "message": f"Error al buscar: {e}", "data": None}


def get_news(topic: str = "") -> dict[str, Any]:
    """
    Obtiene las últimas noticias via DuckDuckGo News.
    Si se especifica topic, busca noticias sobre ese tema.
    """
    try:
        from ddgs import DDGS
        query = topic if topic else "noticias España últimas horas"
        with DDGS() as ddgs:
            hits = list(ddgs.news(query, region="es-es", max_results=5, safesearch="off"))
        if not hits:
            return {"success": False, "message": "No encontré noticias recientes.", "data": None}
        items = []
        for h in hits[:4]:
            title = h.get("title", "").strip()
            source = h.get("source", "").strip()
            if title:
                items.append(f"{title} ({source})" if source else title)
        summary = ". ".join(items)
        label = f"sobre {topic}" if topic else "de última hora"
        return {
            "success": True,
            "message": summary,
            "data": {"topic": topic, "items": items},
        }
    except Exception as e:
        logger.error(f"get_news error: {e}")
        return {"success": False, "message": "No pude obtener las noticias.", "data": None}


def get_crypto_price(coin: str = "bitcoin") -> dict[str, Any]:
    """
    Obtiene el precio actual de una criptomoneda via CoinGecko (sin API key).
    """
    import requests

    # Mapa de nombres hablados → IDs de CoinGecko
    _COIN_MAP = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "solana": "solana", "sol": "solana",
        "cardano": "cardano", "ada": "cardano",
        "ripple": "ripple", "xrp": "ripple",
        "dogecoin": "dogecoin", "doge": "dogecoin",
        "polkadot": "polkadot", "dot": "polkadot",
        "chainlink": "chainlink", "link": "chainlink",
        "matic": "matic-network", "polygon": "matic-network",
        "binance": "binancecoin", "bnb": "binancecoin",
        "litecoin": "litecoin", "ltc": "litecoin",
        "avalanche": "avalanche-2", "avax": "avalanche-2",
        "shiba": "shiba-inu", "shib": "shiba-inu",
    }
    coin_id = _COIN_MAP.get(coin.lower().strip(), coin.lower().strip())
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "eur,usd", "include_24hr_change": "true"},
            timeout=6,
            headers={"Accept": "application/json"},
        )
        data = resp.json()
        if coin_id not in data:
            return {"success": False, "message": f"No encontré la criptomoneda '{coin}', Señor.", "data": None}
        info = data[coin_id]
        eur = info.get("eur", 0)
        usd = info.get("usd", 0)
        change = info.get("eur_24h_change", 0)
        sign = "+" if change >= 0 else ""
        name = coin.capitalize()
        msg = (
            f"{name} cotiza a {eur:,.2f} € ({usd:,.2f} $). "
            f"Cambio en 24h: {sign}{change:.1f}%."
        )
        return {"success": True, "message": msg, "data": info}
    except Exception as e:
        logger.error(f"get_crypto_price error: {e}")
        return {"success": False, "message": "No pude obtener el precio, Señor.", "data": None}


def get_football(query: str = "") -> dict[str, Any]:
    """
    Busca resultados o información de fútbol via DuckDuckGo.
    """
    try:
        from ddgs import DDGS
        search_q = f"fútbol resultado {query}" if query else "resultados fútbol hoy"
        with DDGS() as ddgs:
            hits = list(ddgs.text(search_q, region="es-es", max_results=4,
                                  safesearch="off", timelimit="d"))
        if not hits:
            with DDGS() as ddgs:
                hits = list(ddgs.text(search_q, region="es-es", max_results=4,
                                      safesearch="off", timelimit="w"))
        if not hits:
            return {"success": False, "message": "No encontré resultados de fútbol.", "data": None}
        snippets = [h.get("body", "") for h in hits[:3] if h.get("body")]
        combined = " ".join(snippets)[:2000]
        return {"success": True, "message": combined, "data": {"query": query}}
    except Exception as e:
        logger.error(f"get_football error: {e}")
        return {"success": False, "message": "No pude obtener los resultados de fútbol.", "data": None}


def set_timer(seconds: int = 60) -> dict[str, Any]:
    """Activa un temporizador que avisa con un pitido al terminar."""
    import threading
    import pygame

    def _ring() -> None:
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            import numpy as np
            sr = 22050
            for freq, dur in [(880, 0.15), (1100, 0.15), (1320, 0.25)]:
                t = np.linspace(0, dur, int(sr * dur), False)
                wave = (np.sin(2 * np.pi * freq * t) * 0.7 * 32767).astype(np.int16)
                stereo = np.column_stack([wave, wave])
                sound = pygame.sndarray.make_sound(stereo)
                channel = sound.play()
                while channel and channel.get_busy():
                    pygame.time.wait(20)
        except Exception:
            pass

    threading.Timer(seconds, _ring).start()
    mins = seconds // 60
    secs = seconds % 60
    label = f"{mins}m {secs}s" if mins else f"{secs}s"
    return {"success": True, "message": f"Temporizador de {label} activado.", "data": seconds}


# ----------------------------------------------------------------
# Arranque automático con Windows
# ----------------------------------------------------------------

_STARTUP_FOLDER = Path(os.environ.get("APPDATA", "")) / \
    "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
_VBS_PATH = Path(__file__).parent / "start_viernes.vbs"
_SHORTCUT_PATH = _STARTUP_FOLDER / "VIERNES.lnk"


def enable_autostart() -> dict[str, Any]:
    """Añade VIERNES al arranque automático de Windows."""
    try:
        if _SHORTCUT_PATH.exists():
            return {"success": True,
                    "message": "Ya estaba configurado para arrancar con Windows, Señor.",
                    "data": None}

        if not _VBS_PATH.exists():
            return {"success": False,
                    "message": "No encontré el lanzador start_viernes.vbs.",
                    "data": None}

        import winreg  # noqa: F401 — solo para verificar que estamos en Windows
        # Crear acceso directo vía PowerShell (más fiable que win32com opcional)
        ps_cmd = (
            f'$wsh = New-Object -ComObject WScript.Shell; '
            f'$s = $wsh.CreateShortcut("{_SHORTCUT_PATH}"); '
            f'$s.TargetPath = "wscript.exe"; '
            f'$s.Arguments = """{_VBS_PATH}"""; '
            f'$s.WorkingDirectory = "{_VBS_PATH.parent}"; '
            f'$s.Save()'
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=8
        )
        if result.returncode == 0 and _SHORTCUT_PATH.exists():
            return {"success": True,
                    "message": "Arranque automático activado. Me despertaré solo al encender el PC.",
                    "data": str(_SHORTCUT_PATH)}
        else:
            return {"success": False,
                    "message": f"No pude crear el acceso directo: {result.stderr.decode(errors='ignore')}",
                    "data": None}
    except Exception as e:
        logger.error(f"enable_autostart: {e}")
        return {"success": False, "message": str(e), "data": None}


def disable_autostart() -> dict[str, Any]:
    """Elimina VIERNES del arranque automático de Windows."""
    try:
        if not _SHORTCUT_PATH.exists():
            return {"success": True,
                    "message": "Ya estaba desactivado el arranque automático, Señor.",
                    "data": None}
        _SHORTCUT_PATH.unlink()
        return {"success": True,
                "message": "Arranque automático desactivado. Ahora tendrá que invitarme manualmente.",
                "data": None}
    except Exception as e:
        logger.error(f"disable_autostart: {e}")
        return {"success": False, "message": str(e), "data": None}


# ----------------------------------------------------------------
# Diccionario central de acciones
# brain.py lo consulta por nombre de intención.
# ----------------------------------------------------------------
# Wrappers para acciones de file_manager (necesitan parámetros extraídos en brain.py)
# ----------------------------------------------------------------
def open_app(app_name: str = "") -> dict[str, Any]:
    from file_manager import open_app_by_name
    if not app_name:
        return {"success": False, "message": "No especificó qué aplicación abrir.", "data": None}
    return open_app_by_name(app_name)


def open_file(file_name: str = "") -> dict[str, Any]:
    from file_manager import open_file_by_name
    if not file_name:
        return {"success": False, "message": "No especificó qué archivo abrir.", "data": None}
    return open_file_by_name(file_name)


def read_file(file_name: str = "") -> dict[str, Any]:
    from file_manager import read_file_content
    if not file_name:
        return {"success": False, "message": "No especificó qué archivo leer.", "data": None}
    return read_file_content(file_name)


def create_file_action(file_name: str = "", location: str = "escritorio", content: str = "") -> dict[str, Any]:
    from file_manager import create_file
    if not file_name:
        return {"success": False, "message": "No especificó nombre para el archivo.", "data": None}
    return create_file(file_name, location, content)


def rename_file_action(file_name: str = "", new_name: str = "") -> dict[str, Any]:
    from file_manager import rename_file
    if not file_name or not new_name:
        return {"success": False, "message": "Necesito el nombre actual y el nuevo nombre.", "data": None}
    return rename_file(file_name, new_name)


def move_file_action(file_name: str = "", destination: str = "") -> dict[str, Any]:
    from file_manager import move_file
    if not file_name or not destination:
        return {"success": False, "message": "Necesito el archivo y el destino.", "data": None}
    return move_file(file_name, destination)


def copy_file_action(file_name: str = "", destination: str = "") -> dict[str, Any]:
    from file_manager import copy_file
    if not file_name or not destination:
        return {"success": False, "message": "Necesito el archivo y el destino.", "data": None}
    return copy_file(file_name, destination)


def delete_file_action(file_name: str = "") -> dict[str, Any]:
    from file_manager import delete_file
    if not file_name:
        return {"success": False, "message": "No especificó qué archivo eliminar.", "data": None}
    return delete_file(file_name, to_recycle=True)


def append_to_file_action(file_name: str = "", text: str = "") -> dict[str, Any]:
    from file_manager import append_to_file
    if not file_name:
        return {"success": False, "message": "No especificó a qué archivo añadir.", "data": None}
    return append_to_file(file_name, text)


def replace_in_file_action(file_name: str = "", old: str = "", new: str = "") -> dict[str, Any]:
    from file_manager import replace_in_file
    if not file_name:
        return {"success": False, "message": "No especificó en qué archivo reemplazar.", "data": None}
    return replace_in_file(file_name, old, new)


def write_to_file_action(file_name: str = "", content: str = "") -> dict[str, Any]:
    from file_manager import overwrite_file
    if not file_name:
        return {"success": False, "message": "No especificó en qué archivo escribir.", "data": None}
    return overwrite_file(file_name, content)


def list_directory_action(location: str = "escritorio") -> dict[str, Any]:
    from file_manager import list_directory
    return list_directory(location)


def recent_files_action() -> dict[str, Any]:
    from file_manager import get_recent_files_summary
    return get_recent_files_summary()


# ----------------------------------------------------------------
# Portapapeles
# ----------------------------------------------------------------

def _get_clipboard_text() -> str:
    """Devuelve el texto del portapapeles o cadena vacía."""
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return (win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "").strip()
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        logger.debug(f"Clipboard read error: {e}")
    return ""


def read_clipboard() -> dict[str, Any]:
    """Lee el contenido del portapapeles y lo devuelve para ser leído en voz alta."""
    text = _get_clipboard_text()
    if not text:
        return {"success": False, "message": "El portapapeles está vacío, Señor.", "data": None}
    preview = text[:400] + ("…" if len(text) > 400 else "")
    return {"success": True, "message": preview, "data": text}


def translate_clipboard() -> dict[str, Any]:
    """Lee el portapapeles para su posterior traducción con Ollama."""
    text = _get_clipboard_text()
    if not text:
        return {"success": False, "message": "El portapapeles está vacío, Señor.", "data": None}
    return {"success": True, "message": "", "data": text}


def correct_clipboard() -> dict[str, Any]:
    """Lee el portapapeles para su posterior corrección con Ollama."""
    text = _get_clipboard_text()
    if not text:
        return {"success": False, "message": "El portapapeles está vacío, Señor.", "data": None}
    return {"success": True, "message": "", "data": text}


# ----------------------------------------------------------------
# Notificaciones del sistema
# ----------------------------------------------------------------

def read_notifications(max_count: int = 5) -> dict[str, Any]:
    """Lee las notificaciones recientes desde el Centro de Acción via UserNotificationListener."""
    import asyncio

    async def _fetch():
        from winsdk.windows.ui.notifications.management import UserNotificationListener
        from winsdk.windows.ui.notifications import NotificationKinds
        raw = list(await UserNotificationListener.current.get_notifications_async(
            NotificationKinds.TOAST
        ))
        results = []
        for n in raw[:max_count * 3]:
            try:
                app_name = ""
                try:
                    app_name = n.app_info.display_info.display_name or ""
                except Exception:
                    pass
                texts = []
                for binding in n.notification.visual.bindings:
                    for elem in binding.get_text_elements():
                        t = (elem.text or "").strip()
                        if t:
                            texts.append(t[:80])
                    break
                if not texts:
                    continue
                results.append({
                    "app": app_name,
                    "title": texts[0],
                    "body": texts[1] if len(texts) > 1 else "",
                })
            except Exception:
                continue
        return results[:max_count]

    try:
        loop = asyncio.new_event_loop()
        notifications = loop.run_until_complete(_fetch())
        loop.close()
        return _format_notifications(notifications)
    except Exception as e:
        logger.error(f"read_notifications error: {e}")
        return {"success": False, "message": "No pude leer las notificaciones, Señor.", "data": []}


def _format_notifications(notifications: list[dict]) -> dict[str, Any]:
    """Formatea las notificaciones como mensaje hablable y conciso."""
    if not notifications:
        return {"success": False, "message": "No hay notificaciones recientes, Señor.", "data": []}

    lines = []
    for n in notifications:
        app = n.get("app", "").strip()
        title = n.get("title", "").strip()
        body = n.get("body", "").strip()

        if not title:
            continue

        # Truncar título y cuerpo para que el TTS no sea interminable
        if len(title) > 80:
            title = title[:77] + "..."
        if body and len(body) > 80:
            body = body[:77] + "..."

        if app and title:
            line = f"{app}: {title}"
        else:
            line = title

        if body and body.lower() != title.lower():
            line += f", {body}"
        lines.append(line)

    if not lines:
        return {"success": False, "message": "No hay notificaciones recientes, Señor.", "data": []}

    count = len(lines)
    intro = f"Tiene {count} notificación{'es' if count > 1 else ''}. "
    msg = intro + ". ".join(lines) + "."
    return {"success": True, "message": msg, "data": notifications}


# ----------------------------------------------------------------
ACTIONS: dict[str, Any] = {
    "get_system_stats": get_system_stats,
    "open_vscode": open_vscode,
    "open_terminal": open_terminal,
    "close_distractions": close_distractions,
    "activate_work_mode": activate_work_mode,
    "lock_screen": lock_screen,
    "minimize_all": minimize_all,
    "screenshot": screenshot,
    "analyze_screen": analyze_screen,
    # Volumen
    "volume_up": volume_up,
    "volume_down": volume_down,
    "volume_set": volume_set,
    "mute_volume": mute_volume,
    "play_pause_media": play_pause_media,
    # Apps
    "open_chrome": open_chrome,
    "open_apple_music": open_apple_music,
    # Web e internet
    "web_search": web_search,
    "get_news": get_news,
    "get_crypto": get_crypto_price,
    "get_football": get_football,
    # Timers y sistema
    "set_timer": set_timer,
    "enable_autostart": enable_autostart,
    "disable_autostart": disable_autostart,
    # ── Control de archivos y aplicaciones ──
    "open_app": open_app,
    "open_file": open_file,
    "read_file": read_file,
    "create_file": create_file_action,
    "rename_file": rename_file_action,
    "move_file": move_file_action,
    "copy_file": copy_file_action,
    "delete_file": delete_file_action,
    "append_to_file": append_to_file_action,
    "replace_in_file": replace_in_file_action,
    "write_to_file": write_to_file_action,
    "list_directory": list_directory_action,
    "recent_files": recent_files_action,
    "read_notifications": read_notifications,
    "search_memory": lambda: None,    # gestionado completamente en brain._dispatch_action
    "small_talk": lambda: None,       # gestionado completamente en brain._dispatch_action
    "set_reminder": lambda: None,     # gestionado en brain._dispatch_action via ReminderManager
    "list_reminders": lambda: None,   # gestionado en brain._dispatch_action via ReminderManager
    "enable_dnd": lambda: None,       # gestionado en brain._dispatch_action
    "disable_dnd": lambda: None,      # gestionado en brain._dispatch_action
    # ── Portapapeles ──
    "read_clipboard": read_clipboard,
    "translate_clipboard": translate_clipboard,
    "correct_clipboard": correct_clipboard,
    # ── Control de música avanzado ──
    "next_track": lambda: __import__("music_controller").next_track(),
    "prev_track": lambda: __import__("music_controller").prev_track(),
    "stop_media": lambda: __import__("music_controller").stop_media(),
    "now_playing": lambda: __import__("music_controller").now_playing(),
    "play_genre": lambda genre="": __import__("music_controller").play_genre(genre),
    "play_playlist": lambda name="": __import__("music_controller").play_playlist(name),
    "list_playlists": lambda: __import__("music_controller").get_playlists(),
    "like_track": lambda: __import__("music_controller").like_current_track(),
    "play_song": lambda name="": __import__("music_controller").play_song(name),
}


def warmup_paths() -> None:
    """
    Pre-valida que todas las acciones son callable.
    Llamar desde main.py antes de que llegue el primer comando.
    """
    for name, fn in ACTIONS.items():
        _PATH_CACHE[name] = callable(fn)
    logger.info(f"[ACTIONS] {len(ACTIONS)} acciones pre-cargadas en caché.")

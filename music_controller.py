"""
music_controller.py — Control avanzado de música para VIERNES 2.0.

Capacidades:
  - Siguiente / anterior canción (media keys — funciona con cualquier reproductor)
  - Qué está sonando (Windows SMTC via winsdk o PowerShell fallback)
  - Ponme [género] (Spotify URI → YouTube Music en Chrome como fallback)
  - Subir / bajar volumen de media
  - Like a la canción actual (Spotify)

Compatible con: Spotify, Apple Music, YouTube Music, VLC, Windows Media Player,
               cualquier app que registre sesión en Windows SMTC.
"""
import asyncio
import logging
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import pyautogui

# Desactivar el failsafe de pyautogui para automatización controlada.
# Sin esto, si el ratón roza una esquina de la pantalla durante la automatización
# de Apple Music, lanza FailSafeException y la acción falla silenciosamente.
pyautogui.FAILSAFE = False

logger = logging.getLogger("jarvis.music")

# ================================================================
# Helpers internos
# ================================================================

def _run_detached(cmd: list[str]) -> bool:
    """Lanza proceso desacoplado. Retorna True si tuvo éxito."""
    try:
        subprocess.Popen(
            cmd,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except Exception as e:
        logger.warning(f"_run_detached error: {e}")
        return False


def _is_process_running(name: str) -> bool:
    """Comprueba si un proceso está en ejecución."""
    import psutil
    name_lower = name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name_lower:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def _get_chrome_exe() -> Optional[str]:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


# ================================================================
# Controles de media (universales — cualquier reproductor)
# ================================================================

def next_track() -> dict[str, Any]:
    """Salta a la siguiente canción."""
    try:
        pyautogui.press("nexttrack")
        return {"success": True, "message": "Siguiente canción.", "data": None}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def prev_track() -> dict[str, Any]:
    """Vuelve a la canción anterior."""
    try:
        pyautogui.press("prevtrack")
        return {"success": True, "message": "Canción anterior.", "data": None}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def stop_media() -> dict[str, Any]:
    """Para la reproducción."""
    try:
        pyautogui.press("stop")
        return {"success": True, "message": "Reproducción detenida.", "data": None}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


# ================================================================
# ¿Qué está sonando? — Windows SMTC
# ================================================================

def _get_now_playing_winsdk() -> Optional[dict]:
    """
    Lee la sesión de media activa via Windows.Media.Control (SMTC).
    Requiere winsdk. Funciona con Spotify, Apple Music, YouTube en Chrome, etc.
    """
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )

        async def _fetch():
            sessions = await MediaManager.request_async()
            current = sessions.get_current_session()
            if not current:
                return None
            info = await current.try_get_media_properties_async()
            pb = current.get_playback_info()
            return {
                "title": info.title or "",
                "artist": info.artist or "",
                "album": info.album_title or "",
                "playing": str(pb.playback_status) == "PlaybackStatus.PLAYING",
            }

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_fetch())
        loop.close()
        return result
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"SMTC winsdk error: {e}")
        return None


def _get_now_playing_powershell() -> Optional[dict]:
    """
    Fallback: lee la sesión de media via PowerShell + WinRT.
    Más lento (~1s) pero no requiere winsdk.
    """
    ps_script = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,
         Windows.Media.Control, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethod(
    'AsTask', [System.Reflection.BindingFlags]'Public,Static',
    $null, @([Windows.Foundation.IAsyncOperation[
        Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager
    ]]), $null)
$op = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
$task = $asTask.Invoke($null, @($op))
$task.Wait()
$manager = $task.Result
$session = $manager.GetCurrentSession()
if ($session -eq $null) { Write-Output 'NONE'; exit }
$asTask2 = [System.WindowsRuntimeSystemExtensions].GetMethod(
    'AsTask', [System.Reflection.BindingFlags]'Public,Static',
    $null, @([Windows.Foundation.IAsyncOperation[
        Windows.Media.Control.MediaProperties
    ]]), $null)
$op2 = $session.TryGetMediaPropertiesAsync()
$task2 = $asTask2.Invoke($null, @($op2))
$task2.Wait()
$props = $task2.Result
Write-Output "$($props.Title)|$($props.Artist)|$($props.AlbumTitle)"
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=8,
        )
        out = result.stdout.strip()
        if not out or out == "NONE":
            return None
        parts = out.split("|")
        return {
            "title": parts[0] if len(parts) > 0 else "",
            "artist": parts[1] if len(parts) > 1 else "",
            "album": parts[2] if len(parts) > 2 else "",
            "playing": True,
        }
    except Exception as e:
        logger.debug(f"SMTC PowerShell error: {e}")
        return None


def now_playing() -> dict[str, Any]:
    """
    Devuelve información sobre la canción que está sonando ahora mismo.
    Intenta winsdk primero, PowerShell como fallback.
    """
    info = _get_now_playing_winsdk() or _get_now_playing_powershell()

    if not info:
        return {
            "success": False,
            "message": "No detecté ningún reproductor activo, Señor.",
            "data": None,
        }

    title = info.get("title", "").strip()
    artist = info.get("artist", "").strip()
    album = info.get("album", "").strip()

    if not title:
        return {
            "success": False,
            "message": "Hay un reproductor activo pero no pude leer el nombre de la canción.",
            "data": None,
        }

    if artist:
        msg = f"Ahora suena '{title}' de {artist}"
        if album:
            msg += f", del álbum '{album}'"
        msg += "."
    else:
        msg = f"Ahora suena '{title}'."

    return {"success": True, "message": msg, "data": info}


# ================================================================
# Reproducir por género / artista / búsqueda
# ================================================================

# Géneros comunes → términos de búsqueda mejorados
_GENRE_MAP: dict[str, str] = {
    "rock": "rock clásico",
    "metal": "heavy metal",
    "pop": "pop hits",
    "jazz": "jazz instrumental",
    "electrónica": "electronic music",
    "electronica": "electronic music",
    "reggaeton": "reggaeton hits",
    "trap": "trap español",
    "hip hop": "hip hop",
    "hiphop": "hip hop",
    "rap": "rap español",
    "clásica": "música clásica",
    "clasica": "música clásica",
    "flamenco": "flamenco",
    "salsa": "salsa",
    "bachata": "bachata",
    "r&b": "r&b soul",
    "indie": "indie rock",
    "country": "country music",
    "blues": "blues",
    "funk": "funk",
    "soul": "soul music",
    "lofi": "lofi hip hop",
    "lo-fi": "lofi hip hop",
    "estudio": "lofi study music",
    "concentración": "focus music",
    "concentracion": "focus music",
    "relajar": "música relajante",
    "dormir": "música para dormir",
    "gym": "workout music",
    "entreno": "workout music",
}


def _open_spotify_search(query: str) -> bool:
    """Abre Spotify con una búsqueda usando el URI scheme."""
    try:
        uri = f"spotify:search:{urllib.parse.quote(query)}"
        os.startfile(uri)
        return True
    except Exception as e:
        logger.debug(f"Spotify URI failed: {e}")
        return False


def _open_youtube_music(query: str) -> bool:
    """Abre YouTube Music en Chrome con la búsqueda."""
    url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
    chrome = _get_chrome_exe()
    if chrome:
        return _run_detached([chrome, url])
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception:
        return False


def play_genre(genre: str = "") -> dict[str, Any]:
    """
    Reproduce música de un género o artista específico.
    Intenta Spotify primero (si está instalado), luego YouTube Music.
    """
    if not genre:
        return {"success": False, "message": "No especificó qué música poner.", "data": None}

    genre_lower = genre.lower().strip()
    search_term = _GENRE_MAP.get(genre_lower, genre)

    # Intentar Spotify primero
    spotify_paths = [
        Path(os.environ.get("APPDATA", "")) / "Spotify/Spotify.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/Spotify.exe",
    ]
    spotify_installed = any(p.exists() for p in spotify_paths)

    if spotify_installed:
        if _open_spotify_search(search_term):
            return {
                "success": True,
                "message": f"Buscando {genre} en Spotify, Señor.",
                "data": {"platform": "spotify", "query": search_term},
            }

    # Fallback: YouTube Music en Chrome
    if _open_youtube_music(search_term):
        return {
            "success": True,
            "message": f"Buscando {genre} en YouTube Music, Señor.",
            "data": {"platform": "youtube_music", "query": search_term},
        }

    return {"success": False, "message": f"No pude abrir ningún reproductor para {genre}.", "data": None}


# ================================================================
# Apple Music — Control via Windows UI Automation
# El nuevo Apple Music de Windows no tiene API COM (no es iTunes).
# Usamos UI Automation para interactuar directamente con la interfaz.
# ================================================================

def _normalize_name(text: str) -> str:
    """Normaliza para comparación fuzzy: minúsculas, sin acentos."""
    import unicodedata
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def _find_score(query_norm: str, candidate: str) -> float:
    from difflib import SequenceMatcher
    c_norm = _normalize_name(candidate)
    if query_norm == c_norm:
        return 1.0
    if query_norm in c_norm or c_norm in query_norm:
        return 0.85
    return SequenceMatcher(None, query_norm, c_norm).ratio()


def _ensure_apple_music_open() -> bool:
    """Abre Apple Music si no está corriendo. Retorna True cuando está listo."""
    import time
    if _is_process_running("AppleMusic.exe") or _is_process_running("Music.UI.exe"):
        return True
    # Intentar abrir Apple Music
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps/AppleMusic.exe",
        Path("C:/Program Files/Apple Music/Apple Music.exe"),
        Path("C:/Program Files/iTunes/iTunes.exe"),
    ]
    for p in candidates:
        if p.exists():
            _run_detached([str(p)])
            time.sleep(3)
            return True
    # Fallback: abrir por nombre en Start Menu
    try:
        subprocess.Popen(
            ["explorer.exe", "shell:AppsFolder\\AppleInc.AppleMusic_nzyj5cx40ttqa!AppleMusic"],
            creationflags=subprocess.DETACHED_PROCESS,
        )
        time.sleep(3)
        return True
    except Exception:
        pass
    return False


def _get_apple_music_window():
    """
    Retorna el control de ventana de Apple Music usando uiautomation.
    Retorna None si no se encuentra.
    """
    try:
        import uiautomation as auto
        # Buscar ventana por título o clase
        for title in ("Apple Music", "Music"):
            win = auto.WindowControl(searchDepth=1, Name=title)
            if win.Exists(2):
                return win
        # Búsqueda más amplia
        root = auto.GetRootControl()
        for child in root.GetChildren():
            name = child.Name or ""
            if "Apple Music" in name or "Music" in name:
                return child
        return None
    except ImportError:
        return None


def _collect_playlist_items(win) -> list[tuple[str, Any]]:
    """
    Recorre el árbol de UI de Apple Music buscando items de playlist en el sidebar.
    Retorna lista de (nombre, control).
    """
    import uiautomation as auto
    results = []
    # Palabras del sistema a ignorar en la búsqueda de playlists
    system_names = {
        "inicio", "nueva", "radio", "biblioteca", "destacados",
        "todas las playlists", "canciones favoritas", "artistas",
        "álbumes", "albumes", "canciones", "playlists", "agregado recientemente",
        "home", "new", "listen now", "browse", "library",
    }

    def _walk(ctrl, depth=0):
        if depth > 12:
            return
        try:
            ctrl_type = ctrl.ControlType
            name = (ctrl.Name or "").strip()
            # ListItem o TreeItem con nombre no vacío y no del sistema
            if ctrl_type in (
                auto.ControlType.ListItemControl,
                auto.ControlType.TreeItemControl,
            ) and name and _normalize_name(name) not in system_names:
                results.append((name, ctrl))
            for child in ctrl.GetChildren():
                _walk(child, depth + 1)
        except Exception:
            pass

    _walk(win)
    return results


def get_playlists() -> dict[str, Any]:
    """Lista las playlists disponibles en Apple Music via UI Automation."""
    try:
        import uiautomation  # noqa — solo para verificar que está instalado
    except ImportError:
        return {
            "success": False,
            "message": "Instale uiautomation: py -3.11 -m pip install uiautomation",
            "data": [],
        }

    if not _ensure_apple_music_open():
        return {"success": False, "message": "No pude abrir Apple Music, Señor.", "data": []}

    win = _get_apple_music_window()
    if not win:
        return {"success": False, "message": "No encontré la ventana de Apple Music.", "data": []}

    items = _collect_playlist_items(win)
    names = [n for n, _ in items]
    if not names:
        return {"success": False, "message": "No encontré playlists en el sidebar.", "data": []}

    msg = f"Tiene {len(names)} playlist(s): {', '.join(names[:8])}"
    if len(names) > 8:
        msg += f" y {len(names) - 8} más."
    return {"success": True, "message": msg, "data": names}


def play_playlist(name: str = "") -> dict[str, Any]:
    """
    Reproduce una playlist de Apple Music por nombre usando UI Automation.
    Busca en el sidebar de Apple Music y hace doble clic en la playlist.
    """
    if not name:
        return {"success": False, "message": "No especificó qué playlist reproducir.", "data": None}

    try:
        import uiautomation as auto
        import time
    except ImportError:
        return {
            "success": False,
            "message": "Instale uiautomation: py -3.11 -m pip install uiautomation",
            "data": None,
        }

    if not _ensure_apple_music_open():
        return {"success": False, "message": "No pude abrir Apple Music, Señor.", "data": None}

    win = _get_apple_music_window()
    if not win:
        return {"success": False, "message": "No encontré la ventana de Apple Music.", "data": None}

    # Traer la ventana al frente
    try:
        win.SetFocus()
        time.sleep(0.3)
    except Exception:
        pass

    # Buscar la playlist más parecida al nombre dado
    items = _collect_playlist_items(win)
    if not items:
        return {"success": False, "message": "No encontré playlists en Apple Music.", "data": None}

    name_norm = _normalize_name(name)
    best_score = 0.0
    best_item = None
    best_name = ""

    for item_name, ctrl in items:
        score = _find_score(name_norm, item_name)
        if score > best_score:
            best_score = score
            best_item = ctrl
            best_name = item_name

    if best_item is None or best_score < 0.40:
        available = [n for n, _ in items[:6]]
        hint = f" Sus playlists: {', '.join(available)}." if available else ""
        return {
            "success": False,
            "message": f"No encontré ninguna playlist llamada '{name}'.{hint}",
            "data": None,
        }

    logger.info(f"Playlist encontrada: '{best_name}' (score={best_score:.2f})")

    try:
        # Clic para seleccionar
        best_item.Click()
        time.sleep(0.2)
        # Enter para entrar a la playlist y luego espacio/play para reproducir
        best_item.SendKeys("{ENTER}")
        time.sleep(0.5)
        # Reproducir con la tecla de media Play o Space
        pyautogui.press("space")
        return {
            "success": True,
            "message": f"Reproduciendo '{best_name}', Señor.",
            "data": {"playlist": best_name},
        }
    except Exception as e:
        logger.error(f"play_playlist click error: {e}")
        return {"success": False, "message": f"Encontré la playlist pero no pude abrirla: {e}", "data": None}


def _find_control(ctrl, control_type_name: str, name: str = "", class_name: str = "", depth: int = 0, max_depth: int = 12):
    """Busca un control en el árbol UI por tipo, nombre y/o clase."""
    if depth > max_depth:
        return None
    try:
        type_match = not control_type_name or ctrl.ControlTypeName == control_type_name
        name_match = not name or (ctrl.Name or "").strip() == name
        class_match = not class_name or ctrl.ClassName == class_name
        if type_match and name_match and class_match:
            return ctrl
        for child in ctrl.GetChildren():
            result = _find_control(child, control_type_name, name, class_name, depth + 1, max_depth)
            if result:
                return result
    except Exception:
        pass
    return None


def _find_all_controls(ctrl, control_type_name: str, depth: int = 0, max_depth: int = 12) -> list:
    """Devuelve todos los controles de un tipo dado."""
    results = []
    if depth > max_depth:
        return results
    try:
        if ctrl.ControlTypeName == control_type_name:
            results.append(ctrl)
        for child in ctrl.GetChildren():
            results.extend(_find_all_controls(child, control_type_name, depth + 1, max_depth))
    except Exception:
        pass
    return results


def _get_top_results_first_item(win):
    """
    Navega directamente al primer item del GridView de 'Top resultados'.
    Estrategia: find Top resultados container → find GridView → get first child.
    Esto evita confundir items del sidebar con items del contenido principal.
    """
    # Encontrar el ListItemControl 'Top resultados' (profundidad max 8 desde la ventana)
    top_results = _find_control(win, "ListItemControl", name="Top resultados", max_depth=8)
    if not top_results:
        logger.info("'Top resultados' container no encontrado")
        return None

    # Dentro de él, buscar el ListControl (GridView) que contiene los items reales
    gridview = _find_control(top_results, "ListControl", max_depth=6)
    if not gridview:
        logger.info("GridView dentro de Top resultados no encontrado")
        return None

    # El primer hijo del GridView es el primer resultado
    try:
        children = gridview.GetChildren()
        if children:
            first = children[0]
            logger.info(f"Primer item en Top resultados: '{first.Name}' (tipo={first.ControlTypeName})")
            return first
    except Exception as e:
        logger.debug(f"Error obteniendo children del GridView: {e}")
    return None


def _paste_text_to_focused(text: str) -> None:
    """Pega texto en el control con foco usando portapapeles (soporta Unicode/acentos)."""
    import pyautogui as pag
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        pag.hotkey("ctrl", "v")
    except Exception:
        pag.typewrite(text, interval=0.05)


def _minimize_apple_music(win) -> None:
    """Minimiza la ventana de Apple Music."""
    try:
        import uiautomation as auto
        win.GetWindowPattern().SetWindowVisualState(auto.WindowVisualState.Minimized)
    except Exception:
        import pyautogui as pag
        # Fallback: Win+Down para minimizar ventana activa
        pag.hotkey("win", "down")


def play_song(name: str = "") -> dict[str, Any]:
    """
    Busca una canción en YouTube via yt-dlp y la abre en Chrome.

    Flujo:
    1. yt-dlp busca "ytsearch1:<name>" y devuelve la URL exacta del video
    2. Abre la URL en Chrome (YouTube autoplay activo)
    3. Sin UI Automation — 100% en segundo plano

    Fallback: abre YouTube Music en Chrome con la búsqueda si yt-dlp falla.
    """
    if not name:
        return {"success": False, "message": "No especificó qué canción poner.", "data": None}

    url = _ytdlp_search_url(name)

    if url:
        logger.info(f"play_song: URL obtenida via yt-dlp → {url}")
        ok = _open_in_chrome(url)
        if ok:
            return {
                "success": True,
                "message": f"Reproduciendo '{name}' en YouTube, Señor.",
                "data": {"song": name, "url": url},
            }

    # Fallback: YouTube Music search
    logger.info(f"play_song: yt-dlp falló o Chrome no disponible — fallback YouTube Music")
    query = urllib.parse.quote(name)
    fallback_url = f"https://music.youtube.com/search?q={query}"
    _open_in_chrome(fallback_url)
    return {
        "success": True,
        "message": f"Buscando '{name}' en YouTube Music, Señor.",
        "data": {"song": name, "url": fallback_url},
    }


def _ytdlp_search_url(query: str, timeout: int = 15) -> Optional[str]:
    """
    Llama a yt-dlp para obtener la URL exacta del primer resultado de YouTube.
    Retorna la URL (str) o None si falla.
    """
    try:
        result = subprocess.run(
            [
                "py", "-3.11", "-m", "yt_dlp",
                "--no-playlist",
                "--print", "webpage_url",
                "--no-warnings",
                "--quiet",
                f"ytsearch1:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        url = (result.stdout or "").strip()
        if url and "youtube.com/watch" in url:
            return url
        logger.debug(f"yt-dlp stdout: {url!r} | stderr: {result.stderr[:200]!r}")
    except subprocess.TimeoutExpired:
        logger.warning(f"yt-dlp timeout ({timeout}s) para query: {query!r}")
    except FileNotFoundError:
        logger.warning("yt-dlp no encontrado — instalar: py -3.11 -m pip install yt-dlp")
    except Exception as e:
        logger.warning(f"yt-dlp error: {e}")
    return None


def _open_in_chrome(url: str) -> bool:
    """Abre una URL en Chrome. Retorna True si tuvo éxito."""
    chrome = _get_chrome_exe()
    if chrome:
        try:
            subprocess.Popen(
                [chrome, url],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            return True
        except Exception as e:
            logger.warning(f"Chrome open error: {e}")
    # Fallback: webbrowser
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception as e:
        logger.warning(f"webbrowser.open error: {e}")
    return False


def like_current_track() -> dict[str, Any]:
    """Marca como favorita la canción actual usando el botón 'Favorito' de Apple Music."""
    try:
        import uiautomation as auto
        import time
        win = _get_apple_music_window()
        info = _get_now_playing_winsdk() or _get_now_playing_powershell()
        title = info.get("title", "esta canción") if info else "esta canción"
        if win:
            fav_btn = _find_control(win, "ButtonControl", name="Favorito")
            if fav_btn:
                fav_btn.Click()
                time.sleep(0.2)
                return {"success": True, "message": f"'{title}' marcada como favorita, Señor.", "data": title}
        # Fallback tecla
        pyautogui.hotkey("alt", "l")
        return {"success": True, "message": f"'{title}' marcada como favorita, Señor.", "data": title}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


# ================================================================
# Diccionario de acciones de música
# ================================================================
MUSIC_ACTIONS: dict[str, Any] = {
    "next_track": next_track,
    "prev_track": prev_track,
    "stop_media": stop_media,
    "now_playing": now_playing,
    "play_genre": play_genre,
    "play_playlist": play_playlist,
    "list_playlists": get_playlists,
    "like_track": like_current_track,
    "play_song": play_song,
}

"""
file_manager.py — Control completo del sistema de archivos y aplicaciones para VIERNES 2.0.

Capacidades:
  - Índice de apps instaladas (Start Menu, Program Files, registro de Windows)
  - Búsqueda de archivos por nombre en ubicaciones comunes y todo el disco
  - Apertura de cualquier archivo o aplicación con la app por defecto
  - Lectura y edición de archivos de texto/código
  - Operaciones: renombrar, mover, copiar, eliminar (a papelera), crear
  - Listar contenido de directorios
  - Archivos recientes de Windows
"""
import os
import re
import unicodedata
import shutil
import logging
import subprocess
import winreg
from pathlib import Path
from typing import Any, Optional
from datetime import datetime
from difflib import SequenceMatcher

logger = logging.getLogger("jarvis.file_manager")

# Ejecutables que NUNCA queremos abrir aunque coincidan por nombre
_EXE_BLACKLIST = {
    "update.exe", "updater.exe", "uninstall.exe", "uninst.exe",
    "setup.exe", "installer.exe", "install.exe", "crashhandler.exe",
    "crashreporter.exe", "squirrel.exe", "uninstall000.exe",
}

# ================================================================
# Rutas base de búsqueda
# ================================================================
_HOME = Path.home()
SEARCH_ROOTS: list[Path] = [
    _HOME / "Desktop",
    _HOME / "Documents",
    _HOME / "Downloads",
    _HOME / "Pictures",
    _HOME / "Music",
    _HOME / "Videos",
    _HOME / "OneDrive",
    Path("C:/"),
]

# Profundidad máxima de búsqueda en disco completo
MAX_DEPTH_HOME = 6
MAX_DEPTH_FULL = 4

# Extensiones de texto/código editables
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".env", ".csv", ".html", ".css",
    ".xml", ".bat", ".ps1", ".sh", ".log", ".sql", ".java",
    ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
}

# ================================================================
# Índice de aplicaciones instaladas
# ================================================================
_APP_INDEX: dict[str, str] = {}   # nombre_lower → ruta_exe
_APP_INDEX_BUILT = False


def _resolve_real_exe(path: Path) -> Path:
    """
    Para apps con subcarpetas versionadas (Discord, Slack, etc.) que instalan
    un Update.exe en la raíz, busca el exe real en subcarpetas app-X.X.X.
    Ej: Discord/Update.exe → Discord/app-1.0.9234/Discord.exe
    """
    if path.name.lower() not in _EXE_BLACKLIST:
        return path
    parent = path.parent
    app_name = parent.name  # "Discord", "Slack", etc.
    # Buscar subcarpetas versionadas (app-X.Y.Z)
    versioned = sorted(
        [d for d in parent.iterdir() if d.is_dir() and d.name.startswith("app-")],
        reverse=True,  # más reciente primero
    )
    for vdir in versioned:
        candidate = vdir / f"{app_name}.exe"
        if candidate.exists():
            return candidate
        # Buscar cualquier exe en la subcarpeta que no esté en blacklist
        for exe in vdir.glob("*.exe"):
            if exe.name.lower() not in _EXE_BLACKLIST:
                return exe
    return path  # fallback al original si no encontramos nada mejor


def _scan_directory_for_apps(folder: Path, depth: int = 2) -> dict[str, str]:
    """Escanea una carpeta buscando ejecutables .exe y .lnk (sin blacklist)."""
    found: dict[str, str] = {}
    if not folder.exists() or depth < 0:
        return found
    try:
        for item in folder.iterdir():
            if item.is_file():
                if item.suffix.lower() in (".exe", ".lnk"):
                    # Saltar ejecutables de mantenimiento directamente
                    if item.name.lower() in _EXE_BLACKLIST:
                        continue
                    name = item.stem.lower().strip()
                    if len(name) > 1:
                        found[name] = str(item)
            elif item.is_dir() and depth > 0:
                try:
                    found.update(_scan_directory_for_apps(item, depth - 1))
                except PermissionError:
                    pass
    except PermissionError:
        pass
    return found


def _scan_registry_apps() -> dict[str, str]:
    """Lee aplicaciones registradas en el registro de Windows."""
    found: dict[str, str] = {}
    keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
    ]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in keys:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    val, _ = winreg.QueryValueEx(subkey, "")
                                    name = Path(subkey_name).stem.lower().strip()
                                    if name and val:
                                        found[name] = val
                                except OSError:
                                    pass
                            i += 1
                        except OSError:
                            break
            except OSError:
                pass
    return found


def build_app_index() -> dict[str, str]:
    """
    Construye el índice completo de aplicaciones instaladas.
    Escanea Start Menu, Program Files y registro de Windows.
    """
    global _APP_INDEX, _APP_INDEX_BUILT
    index: dict[str, str] = {}

    # Start Menu (usuario y sistema)
    start_menu_paths = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for sm in start_menu_paths:
        index.update(_scan_directory_for_apps(sm, depth=3))

    # Program Files
    prog_dirs = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WindowsApps",
    ]
    for pd in prog_dirs:
        index.update(_scan_directory_for_apps(pd, depth=2))

    # Registro de Windows
    index.update(_scan_registry_apps())

    # Apps comunes hardcoded como fallback
    common_apps = {
        "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "google chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
        "vlc": r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        "spotify": str(Path(os.environ.get("APPDATA", "")) / "Spotify/Spotify.exe"),
        "discord": str(_resolve_real_exe(Path(os.environ.get("LOCALAPPDATA", "")) / "Discord/Update.exe")),
        "steam": r"C:\Program Files (x86)\Steam\steam.exe",
        "whatsapp": str(Path(os.environ.get("LOCALAPPDATA", "")) / "WhatsApp/WhatsApp.exe"),
        "telegram": str(Path(os.environ.get("APPDATA", "")) / "Telegram Desktop/Telegram.exe"),
        "notepad": "notepad.exe",
        "bloc de notas": "notepad.exe",
        "calculadora": "calc.exe",
        "paint": "mspaint.exe",
        "word": "winword.exe",
        "excel": "excel.exe",
        "powerpoint": "powerpnt.exe",
        "outlook": "outlook.exe",
        "teams": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Teams/current/Teams.exe"),
        "explorer": "explorer.exe",
        "administrador de tareas": "taskmgr.exe",
        "task manager": "taskmgr.exe",
        "panel de control": "control.exe",
        "configuracion": "ms-settings:",
        "photoshop": r"C:\Program Files\Adobe\Adobe Photoshop 2024\Photoshop.exe",
        "premiere": r"C:\Program Files\Adobe\Adobe Premiere Pro 2024\Adobe Premiere Pro.exe",
        "obs": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        "figma": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Figma/Figma.exe"),
    }
    # Solo añadir hardcoded si el exe existe
    for name, path in common_apps.items():
        if path.startswith("ms-") or Path(path).exists():
            index[name] = path

    _APP_INDEX = index
    _APP_INDEX_BUILT = True
    logger.info(f"[APP_INDEX] {len(index)} aplicaciones indexadas.")
    return index


def find_app(query: str) -> Optional[tuple[str, str]]:
    """
    Busca la aplicación más parecida al query.
    Retorna (nombre, ruta) o None si no hay match suficientemente bueno.
    """
    global _APP_INDEX, _APP_INDEX_BUILT
    if not _APP_INDEX_BUILT:
        build_app_index()

    query_lower = query.lower().strip()

    # Coincidencia exacta
    if query_lower in _APP_INDEX:
        return query_lower, _APP_INDEX[query_lower]

    # Coincidencia parcial (el query está contenido en el nombre)
    for name, path in _APP_INDEX.items():
        if query_lower in name or name in query_lower:
            return name, path

    # Fuzzy matching con SequenceMatcher
    best_score = 0.0
    best_match = None
    for name, path in _APP_INDEX.items():
        score = SequenceMatcher(None, query_lower, name).ratio()
        if score > best_score:
            best_score = score
            best_match = (name, path)

    if best_score >= 0.55 and best_match:
        return best_match

    return None


# ================================================================
# Normalización para comparación tolerante
# ================================================================

def _normalize(text: str) -> str:
    """
    Normaliza texto para comparación tolerante:
    - Minúsculas
    - Sin acentos (á→a, é→e, ñ→n…)
    - Guiones bajos y guiones → espacio
    - Espacios múltiples → uno
    """
    text = text.lower()
    # Eliminar acentos
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    # Guiones y guiones bajos → espacio
    text = re.sub(r"[_\-]", " ", text)
    # Espacios múltiples
    text = re.sub(r" +", " ", text).strip()
    return text


def _file_score(query: str, path: Path) -> float:
    """
    Calcula similitud entre query (ya normalizado) y nombre de archivo.
    Prueba el stem completo, sin extensión y con extensión.
    """
    q = _normalize(query)
    stem = _normalize(path.stem)
    full = _normalize(path.name)

    # Coincidencia exacta
    if q == stem or q == full:
        return 1.0
    # El query está contenido en el stem
    if q in stem:
        return 0.90
    # El stem está contenido en el query (usuario dijo nombre completo con extra)
    if stem in q:
        return 0.85
    # Fuzzy sobre stem normalizado
    score = SequenceMatcher(None, q, stem).ratio()
    # Bonus si comparten palabras clave
    q_words = set(q.split())
    s_words = set(stem.split())
    common = q_words & s_words
    if common:
        score = max(score, 0.5 + 0.08 * len(common))
    return score


# ================================================================
# Búsqueda de archivos
# ================================================================

def find_file(
    query: str,
    locations: Optional[list[Path]] = None,
    max_results: int = 5,
    full_disk: bool = False,
) -> list[Path]:
    """
    Busca archivos por nombre en las ubicaciones configuradas.
    Si full_disk=True, escanea todo C:\ (más lento).
    Retorna lista de Paths ordenada por relevancia.
    """
    query_norm = _normalize(query)
    roots = locations or SEARCH_ROOTS[:7]  # Sin C:\ por defecto
    if full_disk:
        roots = SEARCH_ROOTS

    results: list[tuple[float, Path]] = []

    def _walk(root: Path, max_depth: int, current_depth: int = 0) -> None:
        if current_depth > max_depth:
            return
        try:
            for item in root.iterdir():
                if item.is_file():
                    # Comparación normalizada: ignora acentos, guiones bajos, mayúsculas
                    item_norm = _normalize(item.stem)
                    # Filtro rápido: al menos una palabra del query aparece en el nombre
                    q_words = query_norm.split()
                    if any(w in item_norm for w in q_words) or query_norm in item_norm:
                        score = _file_score(query, item)
                        if score >= 0.40:
                            results.append((score, item))
                            if len(results) >= max_results * 15:
                                return
                elif item.is_dir() and not item.name.startswith("."):
                    try:
                        _walk(item, max_depth, current_depth + 1)
                    except PermissionError:
                        pass
        except PermissionError:
            pass

    for root in roots:
        if not root.exists():
            continue
        depth = MAX_DEPTH_FULL if root == Path("C:/") else MAX_DEPTH_HOME
        _walk(root, depth)

    # Ordenar por score descendente, luego por modificación reciente
    results.sort(key=lambda x: (-x[0], -x[1].stat().st_mtime if x[1].exists() else 0))
    return [p for _, p in results[:max_results]]


def get_recent_files(n: int = 10) -> list[Path]:
    """
    Retorna los n archivos más recientemente usados desde la carpeta Recent de Windows.
    """
    recent_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Recent"
    if not recent_dir.exists():
        return []
    files = []
    try:
        for item in recent_dir.iterdir():
            if item.suffix.lower() == ".lnk":
                files.append((item.stat().st_mtime, item))
    except Exception:
        pass
    files.sort(reverse=True)
    return [p for _, p in files[:n]]


# ================================================================
# Operaciones sobre archivos
# ================================================================

def open_path(path: str | Path) -> dict[str, Any]:
    """Abre un archivo o directorio con la aplicación por defecto de Windows."""
    p = Path(path)
    if not p.exists():
        return {"success": False, "message": f"No encontré '{path}', Señor.", "data": None}
    try:
        os.startfile(str(p))
        return {"success": True, "message": f"Abriendo {p.name}.", "data": str(p)}
    except Exception as e:
        logger.error(f"open_path error: {e}")
        return {"success": False, "message": str(e), "data": None}


def open_app_by_name(app_name: str) -> dict[str, Any]:
    """Busca y abre una aplicación por nombre."""
    match = find_app(app_name)
    if not match:
        return {
            "success": False,
            "message": f"No encontré ninguna aplicación llamada '{app_name}', Señor.",
            "data": None,
        }
    name, path = match
    # Resolver exe real si el índice apunta a un Update.exe o similar
    if not path.startswith("ms-"):
        resolved = _resolve_real_exe(Path(path))
        path = str(resolved)
    logger.info(f"Abriendo app: {name} → {path}")
    try:
        if path.startswith("ms-"):
            subprocess.Popen(["explorer.exe", path], creationflags=subprocess.DETACHED_PROCESS)
        else:
            exe = Path(path)
            if not exe.exists():
                return {"success": False, "message": f"No encontré el ejecutable de {name}, Señor.", "data": None}
            if exe.suffix.lower() == ".lnk":
                os.startfile(str(exe))
            else:
                subprocess.Popen(
                    [str(exe)],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
        return {"success": True, "message": f"{name.title()} abierto, Señor.", "data": path}
    except Exception as e:
        logger.error(f"open_app_by_name error: {e}")
        return {"success": False, "message": f"No pude abrir {name}: {e}", "data": None}


def open_file_by_name(file_name: str, full_disk: bool = False) -> dict[str, Any]:
    """Busca y abre un archivo por nombre aproximado."""
    results = find_file(file_name, full_disk=full_disk)
    if not results:
        if not full_disk:
            # Reintentar en disco completo
            results = find_file(file_name, full_disk=True)
    if not results:
        return {
            "success": False,
            "message": f"No encontré ningún archivo llamado '{file_name}', Señor.",
            "data": None,
        }
    best = results[0]
    return open_path(best)


def read_file_content(file_name: str) -> dict[str, Any]:
    """Lee el contenido de un archivo de texto y lo retorna."""
    results = find_file(file_name)
    if not results:
        results = find_file(file_name, full_disk=True)
    if not results:
        return {
            "success": False,
            "message": f"No encontré el archivo '{file_name}', Señor.",
            "data": None,
        }
    path = results[0]
    if path.suffix.lower() not in TEXT_EXTENSIONS and path.suffix != "":
        return {
            "success": False,
            "message": f"'{path.name}' no es un archivo de texto que pueda leer, Señor.",
            "data": None,
        }
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        preview = content[:500]
        return {
            "success": True,
            "message": f"Archivo '{path.name}' leído ({len(content)} caracteres).",
            "data": {"path": str(path), "content": content, "preview": preview},
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def write_file_content(path: str | Path, content: str) -> dict[str, Any]:
    """Escribe contenido en un archivo de texto."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True, "message": f"Archivo '{p.name}' guardado.", "data": str(p)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def create_file(file_name: str, location: str = "desktop", content: str = "") -> dict[str, Any]:
    """Crea un nuevo archivo en la ubicación indicada (sin sobrescribir uno existente)."""
    location_map = {
        "desktop": _HOME / "Desktop",
        "escritorio": _HOME / "Desktop",
        "documents": _HOME / "Documents",
        "documentos": _HOME / "Documents",
        "downloads": _HOME / "Downloads",
        "descargas": _HOME / "Downloads",
    }
    folder = location_map.get(location.lower(), _HOME / "Desktop")
    path = folder / file_name
    # Añadir extensión .txt si no tiene
    if not path.suffix:
        path = path.with_suffix(".txt")
    # NO sobrescribir silenciosamente: si existe, buscar un nombre libre "(2)", "(3)"…
    if path.exists():
        stem, suffix, parent = path.stem, path.suffix, path.parent
        n = 2
        while (parent / f"{stem} ({n}){suffix}").exists():
            n += 1
        path = parent / f"{stem} ({n}){suffix}"
    return write_file_content(path, content)


# ================================================================
# Edición de contenido por voz (con backup .bak automático)
# ================================================================

def _backup_file(path: Path) -> None:
    """Crea una copia .bak antes de modificar un archivo (red de seguridad)."""
    try:
        if path.exists():
            shutil.copy2(str(path), str(path) + ".bak")
    except Exception as e:
        logger.warning(f"No se pudo crear backup de {path}: {e}")


def _is_editable_text(path: Path) -> bool:
    """Solo permitimos editar archivos de texto (no corromper binarios)."""
    return path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == ""


def _resolve_existing(file_name: str) -> Optional[Path]:
    """Busca un archivo existente por nombre (carpetas comunes → disco completo)."""
    results = find_file(file_name) or find_file(file_name, full_disk=True)
    return results[0] if results else None


def append_to_file(file_name: str, text: str) -> dict[str, Any]:
    """Añade texto al final de un archivo de texto existente."""
    if not text:
        return {"success": False, "message": "No me dijo qué añadir, Señor.", "data": None}
    path = _resolve_existing(file_name)
    if not path:
        return {"success": False, "message": f"No encontré ningún archivo llamado '{file_name}', Señor.", "data": None}
    if not _is_editable_text(path):
        return {"success": False, "message": f"'{path.name}' no es un archivo de texto que pueda editar, Señor.", "data": None}
    try:
        _backup_file(path)
        existing = path.read_text(encoding="utf-8", errors="replace")
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        path.write_text(existing + sep + text + "\n", encoding="utf-8")
        return {"success": True, "message": f"Añadido a {path.name}, Señor.", "data": str(path)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def replace_in_file(file_name: str, old: str, new: str) -> dict[str, Any]:
    """Reemplaza texto dentro de un archivo de texto existente."""
    if not old:
        return {"success": False, "message": "No me dijo qué texto reemplazar, Señor.", "data": None}
    path = _resolve_existing(file_name)
    if not path:
        return {"success": False, "message": f"No encontré ningún archivo llamado '{file_name}', Señor.", "data": None}
    if not _is_editable_text(path):
        return {"success": False, "message": f"'{path.name}' no es un archivo de texto que pueda editar, Señor.", "data": None}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        # Búsqueda tolerante a mayúsculas si no hay coincidencia exacta
        count = content.count(old)
        if count == 0:
            import re as _re
            pattern = _re.compile(_re.escape(old), _re.IGNORECASE)
            count = len(pattern.findall(content))
            if count == 0:
                return {"success": False, "message": f"No encontré '{old}' en {path.name}, Señor.", "data": None}
            _backup_file(path)
            content = pattern.sub(new, content)
        else:
            _backup_file(path)
            content = content.replace(old, new)
        path.write_text(content, encoding="utf-8")
        veces = "vez" if count == 1 else "veces"
        return {"success": True, "message": f"Reemplazado en {path.name}, {count} {veces}, Señor.", "data": str(path)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def overwrite_file(file_name: str, content: str) -> dict[str, Any]:
    """
    Escribe contenido en un archivo (sobrescribe el existente con backup, o crea
    uno nuevo en el Escritorio si no existe). Usado por el modo dictado.
    """
    path = _resolve_existing(file_name)
    if path:
        if not _is_editable_text(path):
            return {"success": False, "message": f"'{path.name}' no es un archivo de texto que pueda editar, Señor.", "data": None}
        _backup_file(path)
    else:
        # No existe → crearlo en el Escritorio
        path = _HOME / "Desktop" / file_name
        if not path.suffix:
            path = path.with_suffix(".txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")
        return {"success": True, "message": f"Guardado en {path.name}, Señor.", "data": str(path)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def rename_file(file_name: str, new_name: str) -> dict[str, Any]:
    """Busca un archivo y lo renombra."""
    results = find_file(file_name)
    if not results:
        results = find_file(file_name, full_disk=True)
    if not results:
        return {"success": False, "message": f"No encontré '{file_name}'.", "data": None}
    old_path = results[0]
    new_path = old_path.parent / (new_name if "." in new_name else new_name + old_path.suffix)
    try:
        old_path.rename(new_path)
        return {
            "success": True,
            "message": f"Renombrado '{old_path.name}' a '{new_path.name}'.",
            "data": str(new_path),
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def move_file(file_name: str, destination: str) -> dict[str, Any]:
    """Mueve un archivo a una carpeta destino."""
    results = find_file(file_name)
    if not results:
        return {"success": False, "message": f"No encontré '{file_name}'.", "data": None}
    src = results[0]

    dest_map = {
        "desktop": _HOME / "Desktop",
        "escritorio": _HOME / "Desktop",
        "documents": _HOME / "Documents",
        "documentos": _HOME / "Documents",
        "downloads": _HOME / "Downloads",
        "descargas": _HOME / "Downloads",
        "pictures": _HOME / "Pictures",
        "imágenes": _HOME / "Pictures",
        "music": _HOME / "Music",
        "música": _HOME / "Music",
    }
    dest_folder = dest_map.get(destination.lower(), Path(destination))
    dest_path = dest_folder / src.name

    try:
        dest_folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_path))
        return {
            "success": True,
            "message": f"'{src.name}' movido a {dest_folder.name}.",
            "data": str(dest_path),
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def copy_file(file_name: str, destination: str) -> dict[str, Any]:
    """Copia un archivo a una carpeta destino."""
    results = find_file(file_name)
    if not results:
        return {"success": False, "message": f"No encontré '{file_name}'.", "data": None}
    src = results[0]

    dest_map = {
        "desktop": _HOME / "Desktop", "escritorio": _HOME / "Desktop",
        "documents": _HOME / "Documents", "documentos": _HOME / "Documents",
        "downloads": _HOME / "Downloads", "descargas": _HOME / "Downloads",
    }
    dest_folder = dest_map.get(destination.lower(), Path(destination))
    dest_path = dest_folder / src.name

    try:
        dest_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest_path))
        return {
            "success": True,
            "message": f"'{src.name}' copiado a {dest_folder.name}.",
            "data": str(dest_path),
        }
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def delete_file(file_name: str, to_recycle: bool = True) -> dict[str, Any]:
    """Elimina un archivo (a la papelera si to_recycle=True)."""
    results = find_file(file_name)
    if not results:
        return {"success": False, "message": f"No encontré '{file_name}'.", "data": None}
    path = results[0]
    try:
        if to_recycle:
            # send2trash: mueve a la papelera sin shell (evita inyección por la ruta)
            from send2trash import send2trash
            send2trash(str(path))
        else:
            path.unlink()
        return {"success": True, "message": f"'{path.name}' eliminado.", "data": str(path)}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def list_directory(location: str = "desktop") -> dict[str, Any]:
    """Lista el contenido de una carpeta."""
    loc_map = {
        "desktop": _HOME / "Desktop", "escritorio": _HOME / "Desktop",
        "documents": _HOME / "Documents", "documentos": _HOME / "Documents",
        "downloads": _HOME / "Downloads", "descargas": _HOME / "Downloads",
        "pictures": _HOME / "Pictures", "imágenes": _HOME / "Pictures",
        "music": _HOME / "Music", "música": _HOME / "Music",
        "videos": _HOME / "Videos",
        "c:": Path("C:/"), "disco": Path("C:/"),
    }
    folder = loc_map.get(location.lower().strip(), Path(location))
    if not folder.exists():
        return {"success": False, "message": f"No encontré la carpeta '{location}'.", "data": None}

    try:
        items = list(folder.iterdir())
        files = [i for i in items if i.is_file()]
        dirs = [i for i in items if i.is_dir()]
        summary = (
            f"En {folder.name} hay {len(dirs)} carpeta(s) y {len(files)} archivo(s). "
        )
        if files:
            names = [f.name for f in sorted(files, key=lambda x: -x.stat().st_mtime)[:8]]
            summary += f"Archivos recientes: {', '.join(names)}."
        return {"success": True, "message": summary, "data": {"files": files, "dirs": dirs}}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def get_recent_files_summary(n: int = 5) -> dict[str, Any]:
    """Retorna un resumen de los archivos más recientemente usados."""
    recent = get_recent_files(n)
    if not recent:
        return {"success": False, "message": "No encontré archivos recientes.", "data": None}
    names = [p.stem for p in recent]
    msg = f"Sus archivos más recientes son: {', '.join(names)}."
    return {"success": True, "message": msg, "data": [str(p) for p in recent]}

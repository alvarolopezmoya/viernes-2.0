"""
reminder_manager.py — Gestión de recordatorios para VIERNES 2.0.

Persiste recordatorios en SQLite (tabla 'reminders').
Al arrancar carga los pendientes y los reactiva con threading.Timer.
Soporta:
  - Tiempo relativo: "en 20 minutos", "en 2 horas"
  - Tiempo absoluto hoy: "a las 15:30", "a las 3 de la tarde"
  - Mañana: "mañana a las 9"
"""
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("jarvis.reminders")


class ReminderManager:
    """
    Gestiona recordatorios persistentes.
    Los recordatorios se guardan en SQLite y se recargan al arrancar.
    Al dispararse llama al callback on_fire(message: str).
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS reminders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message     TEXT    NOT NULL,
        fire_at     TEXT    NOT NULL,
        created_at  TEXT    NOT NULL,
        done        INTEGER NOT NULL DEFAULT 0
    );
    """

    def __init__(self, db_path: Path, on_fire: Callable[[str], None]) -> None:
        self._db_path = str(db_path)
        self._on_fire = on_fire
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

        conn = self._conn()
        conn.executescript(self._SCHEMA)
        conn.commit()
        conn.close()

        self._reload_pending()

    # ----------------------------------------------------------------
    # DB helpers
    # ----------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _reload_pending(self) -> None:
        """Carga recordatorios pendientes al arrancar y los reactiva."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, message, fire_at FROM reminders "
            "WHERE done=0 AND fire_at > datetime('now') "
            "ORDER BY fire_at ASC"
        ).fetchall()
        conn.close()
        for rid, msg, fire_at_str in rows:
            try:
                fire_at = datetime.fromisoformat(fire_at_str)
                self._schedule(rid, msg, fire_at)
                logger.info(f"Recordatorio #{rid} recargado: '{msg}' a las {fire_at_str}")
            except Exception as e:
                logger.warning(f"No se pudo recargar recordatorio #{rid}: {e}")

    # ----------------------------------------------------------------
    # API pública
    # ----------------------------------------------------------------

    def add(self, message: str, fire_at: datetime) -> dict:
        """
        Añade un recordatorio y lo programa.
        Retorna dict con success, message y data.
        """
        now = datetime.now()
        if fire_at <= now:
            return {
                "success": False,
                "message": "Esa hora ya pasó, Señor. Intente con una hora futura.",
                "data": None,
            }

        seconds = (fire_at - now).total_seconds()
        if seconds > 86400 * 7:  # Más de 7 días
            return {
                "success": False,
                "message": "Solo puedo programar recordatorios hasta 7 días vista, Señor.",
                "data": None,
            }

        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO reminders (message, fire_at, created_at) "
            "VALUES (?, datetime(?), datetime('now'))",
            (message, fire_at.isoformat()),
        )
        rid = cur.lastrowid
        conn.commit()
        conn.close()

        self._schedule(rid, message, fire_at)

        # Formato legible para la confirmación
        time_str = fire_at.strftime("%H:%M")
        date_str = ""
        if fire_at.date() != now.date():
            date_str = f" del {fire_at.strftime('%d/%m')}"

        mins = int(seconds / 60)
        if mins < 60:
            when = f"en {mins} minuto{'s' if mins != 1 else ''}"
        elif mins < 1440:
            h = mins // 60
            m = mins % 60
            when = f"en {h}h{f' {m}min' if m else ''}"
        else:
            when = f"el {fire_at.strftime('%d/%m')} a las {time_str}"

        return {
            "success": True,
            "message": (
                f"Recordatorio programado para las {time_str}{date_str} "
                f"({when}): {message}."
            ),
            "data": {"id": rid, "fire_at": fire_at.isoformat(), "message": message},
        }

    def list_pending(self) -> list[dict]:
        """Retorna los recordatorios pendientes."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, message, fire_at FROM reminders "
            "WHERE done=0 AND fire_at > datetime('now') "
            "ORDER BY fire_at ASC LIMIT 10"
        ).fetchall()
        conn.close()
        return [{"id": r[0], "message": r[1], "fire_at": r[2]} for r in rows]

    def cancel(self, rid: int) -> bool:
        """Cancela un recordatorio por ID."""
        with self._lock:
            timer = self._timers.pop(rid, None)
            if timer:
                timer.cancel()
        conn = self._conn()
        conn.execute("UPDATE reminders SET done=1 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return timer is not None

    # ----------------------------------------------------------------
    # Scheduling interno
    # ----------------------------------------------------------------

    def _schedule(self, rid: int, message: str, fire_at: datetime) -> None:
        """Programa un threading.Timer para el momento indicado."""
        delay = max(0.0, (fire_at - datetime.now()).total_seconds())
        timer = threading.Timer(delay, self._fire, args=(rid, message))
        timer.daemon = True
        timer.name = f"reminder_{rid}"
        with self._lock:
            self._timers[rid] = timer
        timer.start()
        logger.info(f"Recordatorio #{rid} programado en {delay:.0f}s: '{message}'")

    def _fire(self, rid: int, message: str) -> None:
        """Callback cuando se dispara el timer."""
        with self._lock:
            self._timers.pop(rid, None)
        # Marcar como hecho en DB
        try:
            conn = self._conn()
            conn.execute("UPDATE reminders SET done=1 WHERE id=?", (rid,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"No se pudo marcar recordatorio #{rid} como hecho: {e}")

        logger.info(f"Recordatorio #{rid} disparado: '{message}'")
        try:
            self._on_fire(message)
        except Exception as e:
            logger.error(f"Error en callback de recordatorio #{rid}: {e}")


# ================================================================
# Parser de tiempo en lenguaje natural
# ================================================================

def parse_reminder_time(text: str) -> Optional[datetime]:
    """
    Extrae datetime de frases como:
      "en 20 minutos", "en 2 horas", "a las 15:30",
      "a las 3 de la tarde", "mañana a las 9", "en media hora"
    Retorna None si no puede parsear.
    """
    t = text.lower().strip()
    now = datetime.now()

    # ── Relativo: "en X minutos/horas" ──────────────────────────────
    m = re.search(r"en\s+(\d+)\s*(minuto|min|hora|h)\w*", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return now + delta

    # ── Relativo: "en media hora" ────────────────────────────────────
    if "media hora" in t:
        return now + timedelta(minutes=30)

    # ── Relativo: "en un cuarto de hora" ─────────────────────────────
    if "cuarto de hora" in t or "quince minutos" in t:
        return now + timedelta(minutes=15)

    # ── Absoluto: extraer hora y minuto ──────────────────────────────
    # Soporta: "15:30", "3:30", "a las 3", "a las 15"
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?", t)
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2)) if time_match.group(2) else 0

    # Ajuste AM/PM hablado
    if "tarde" in t or "noche" in t:
        if hour < 12:
            hour += 12
    elif "mañana" in t and hour >= 12 and "de la mañana" in t:
        hour = hour  # ya en formato 24h
    elif hour <= 6 and "mañana" not in t:
        # Horas ambiguas (1-6) → tarde si son horario de tarde
        # Por defecto dejamos como está y confiamos en el contexto
        pass

    # ── ¿Es mañana? ──────────────────────────────────────────────────
    base_date = now.date()
    if "mañana" in t:
        base_date = (now + timedelta(days=1)).date()

    fire_at = datetime(base_date.year, base_date.month, base_date.day, hour, minute)

    # Si la hora ya pasó hoy → programar para mañana automáticamente
    if fire_at <= now and "mañana" not in t:
        fire_at += timedelta(days=1)

    return fire_at


def extract_reminder_message(text: str) -> str:
    """
    Extrae el mensaje del recordatorio de la frase completa.
    Ej: "recuérdame a las 15:30 tomar agua" → "tomar agua"
    """
    t = text.lower().strip()

    # Quitar el trigger del recordatorio
    for trigger in (
        "recuérdame", "recuerdame", "pon un recordatorio",
        "pon recordatorio", "acuérdame", "acuerdame",
        "recuérdame que", "recuerdame que", "avísame", "avisame",
        "ponme un recordatorio", "ponme recordatorio",
    ):
        t = t.replace(trigger, "").strip()

    # Quitar la parte de tiempo (todo lo que está antes del mensaje)
    # Patrones de tiempo a eliminar
    time_patterns = [
        r"en\s+\d+\s*(?:minutos?|mins?|horas?|h)\w*",
        r"en\s+media\s+hora",
        r"en\s+(?:un\s+)?cuarto\s+de\s+hora",
        r"(?:mañana\s+)?a\s+las\s+\d{1,2}(?::\d{2})?\s*(?:de\s+la\s+(?:mañana|tarde|noche))?",
        r"a\s+las\s+\d{1,2}(?::\d{2})?",
        r"para\s+las\s+\d{1,2}(?::\d{2})?",
        r"a\s+\d{1,2}(?::\d{2})?",
    ]
    for pattern in time_patterns:
        t = re.sub(pattern, "", t).strip()

    # Limpiar conectores residuales al inicio
    for connector in ("para", "que", "de", "por", "sobre", ",", "-"):
        if t.startswith(connector + " "):
            t = t[len(connector):].strip()
        elif t == connector:
            t = ""

    return t.strip(" .,¿?") or "recordatorio sin mensaje"

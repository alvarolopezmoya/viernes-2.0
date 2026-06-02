"""
notification_watcher.py — Monitor de notificaciones en tiempo real para VIERNES 2.0.

Usa Windows UserNotificationListener (winsdk) corriendo en un único event loop
persistente para evitar el spam de "asyncio: Using selector" que aparece al
crear loops nuevos en cada poll.

Si el listener no está disponible, hace fallback a wpndatabase.db.
"""
import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("jarvis.notif_watcher")

POLL_INTERVAL = 3.0

_APP_ALIASES: dict[str, str] = {
    "WhatsAppDesktop": "WhatsApp",
    "windowscommunicationsapps": "Correo",
    "MicrosoftEdge": "Edge",
    "SkyDrive": "OneDrive",
    "ScreenSketch": "Recortes",
    "MicrosoftTeams": "Teams",
    "DiscordPTB": "Discord",
    "Discord": "Discord",
    "Telegram": "Telegram",
}

_MESSAGING_APPS = {"whatsapp", "telegram", "discord", "teams", "correo", "instagram", "signal"}

_SKIP_APPS = {
    "backgroundtaskhost", "runtimebroker", "settingssynchost",
    "shellexperiencehost", "startmenuexperiencehost",
}


class NotificationWatcher:

    def __init__(self, on_notification: Callable[[str], None]) -> None:
        self._callback = on_notification
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_ids: set[int] = set()
        self._last_db_id: int = 0
        self._use_listener: bool = False
        self._db_path = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft/Windows/Notifications/wpndatabase.db"
        )

    # ── API pública ───────────────────────────────────────────────────

    def start(self) -> None:
        # Comprobar acceso al listener de forma síncrona antes de arrancar el hilo
        self._use_listener = self._check_listener_access()

        if self._use_listener:
            logger.info("NotificationWatcher: UserNotificationListener activo")
        elif self._db_path.exists():
            self._last_db_id = self._get_max_db_id()
            logger.info(f"NotificationWatcher: DB fallback activo (last_id={self._last_db_id})")
        else:
            logger.warning("NotificationWatcher: sin método disponible, deshabilitado")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="notif_watcher"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── Hilo principal — event loop único ────────────────────────────

    def _run(self) -> None:
        """
        Corre toda la lógica async en un único event loop persistente.
        Evita crear loops nuevos en cada poll (causa spam en logs).
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_loop())
        finally:
            loop.close()

    async def _async_loop(self) -> None:
        # Inicializar IDs ya vistos para no anunciar notificaciones antiguas
        if self._use_listener:
            await self._init_seen_ids_async()

        while not self._stop.is_set():
            try:
                if self._use_listener:
                    await self._check_listener_async()
                else:
                    self._check_db()
            except Exception as e:
                logger.debug(f"Watcher poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    # ── UserNotificationListener ──────────────────────────────────────

    def _check_listener_access(self) -> bool:
        """Verifica y solicita acceso al listener. Síncrono, llamar antes del hilo."""
        try:
            from winsdk.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )
            # Usar un loop temporal solo para la solicitud de acceso
            loop = asyncio.new_event_loop()
            status = loop.run_until_complete(
                UserNotificationListener.current.request_access_async()
            )
            loop.close()
            granted = status == UserNotificationListenerAccessStatus.ALLOWED
            logger.info(f"UserNotificationListener acceso: {'CONCEDIDO' if granted else 'DENEGADO'}")
            return granted
        except Exception as e:
            logger.info(f"UserNotificationListener no disponible: {e}")
            return False

    async def _init_seen_ids_async(self) -> None:
        """Registra las notificaciones actuales para no anunciarlas al arrancar."""
        notifs = await self._fetch_all_async()
        self._seen_ids = {n["id"] for n in notifs}
        logger.info(f"NotificationWatcher: {len(self._seen_ids)} notificaciones previas ignoradas")

    async def _check_listener_async(self) -> None:
        """Detecta notificaciones nuevas en el Centro de Acción."""
        notifs = await self._fetch_all_async()
        for n in notifs:
            nid = n["id"]
            if nid in self._seen_ids:
                continue
            self._seen_ids.add(nid)
            msg = self._format(n["app"], n["title"], n["body"])
            if msg:
                logger.info(f"[NOTIF] nueva id={nid} app={n['app']!r}: {n['title'][:50]!r}")
                self._callback(msg)

    async def _fetch_all_async(self) -> list[dict]:
        """Obtiene todas las notificaciones toast del Centro de Acción."""
        try:
            from winsdk.windows.ui.notifications.management import UserNotificationListener
            from winsdk.windows.ui.notifications import NotificationKinds

            raw = list(
                await UserNotificationListener.current.get_notifications_async(
                    NotificationKinds.TOAST
                )
            )
            results = []
            for n in raw:
                parsed = self._parse_user_notif(n)
                if parsed:
                    results.append(parsed)
            return results
        except Exception as e:
            logger.debug(f"fetch_all error: {e}")
            return []

    @staticmethod
    def _parse_user_notif(notif) -> Optional[dict]:
        try:
            app_name = ""
            try:
                app_name = notif.app_info.display_info.display_name or ""
            except Exception:
                pass
            if not app_name:
                try:
                    aumid = notif.app_info.app_user_model_id or ""
                    app_name = NotificationWatcher._app_from_id(aumid)
                except Exception:
                    pass

            # API real: visual.bindings → get_text_elements() → .text
            texts = []
            try:
                for binding in notif.notification.visual.bindings:
                    for elem in binding.get_text_elements():
                        t = (elem.text or "").strip()
                        if t:
                            texts.append(t[:100])
                    break  # solo primer binding
            except Exception:
                pass

            if not texts:
                return None

            return {
                "id": notif.id,
                "app": app_name,
                "title": texts[0],
                "body": texts[1] if len(texts) > 1 else "",
            }
        except Exception:
            return None

    # ── Fallback: DB polling ──────────────────────────────────────────

    def _check_db(self) -> None:
        rows = self._db_query("""
            SELECT n.Id, h.PrimaryId, n.Payload
            FROM Notification n
            LEFT JOIN NotificationHandler h ON h.RecordId = n.HandlerId
            WHERE n.Id > ? AND n.Payload IS NOT NULL
            ORDER BY n.Id ASC
        """, (self._last_db_id,))

        for notif_id, primary_id, payload in rows:
            self._last_db_id = max(self._last_db_id, notif_id)
            msg = self._parse_db_payload(primary_id, payload)
            if msg:
                logger.info(f"[NOTIF DB] id={notif_id}: {msg[:80]}")
                self._callback(msg)

    def _parse_db_payload(self, primary_id, payload) -> Optional[str]:
        if not payload:
            return None
        try:
            s = payload.decode("utf-8", errors="ignore") if isinstance(payload, (bytes, bytearray)) else str(payload)
            root = ET.fromstring(s)
            if root.tag != "toast":
                return None
            texts = [e.text.strip() for e in root.iter("text") if e.text and e.text.strip()]
            if not texts:
                return None
            app = self._app_from_id(str(primary_id or ""))
            return self._format(app, texts[0][:80], texts[1][:80] if len(texts) > 1 else "")
        except Exception:
            return None

    def _get_max_db_id(self) -> int:
        rows = self._db_query("SELECT MAX(Id) FROM Notification")
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    def _db_query(self, sql: str, params: tuple = ()) -> list:
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            shutil.copy2(str(self._db_path), tmp.name)
            conn = sqlite3.connect(tmp.name)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return rows
        except Exception as e:
            logger.debug(f"DB query error: {e}")
            return []
        finally:
            if tmp:
                try:
                    Path(tmp.name).unlink(missing_ok=True)
                except Exception:
                    pass

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _app_from_id(app_id: str) -> str:
        if "!" in app_id:
            pkg = app_id.split("!")[0].split(".")[-1]
            app = pkg.split("_")[0]
        elif "\\" in app_id:
            app = Path(app_id).stem
        elif "." in app_id:
            app = app_id.split(".")[-1]
        else:
            app = app_id
        return _APP_ALIASES.get(app, app).strip()

    @staticmethod
    def _format(app: str, title: str, body: str) -> Optional[str]:
        if not title:
            return None
        if app and app.lower() in _SKIP_APPS:
            return None
        is_msg = app.lower() in _MESSAGING_APPS if app else False
        if app and body:
            if is_msg:
                return f"Señor, tiene un mensaje de {app}. {title} dice: {body}."
            return f"Señor, notificación de {app}. {title}: {body}."
        elif app:
            if is_msg:
                return f"Señor, tiene un mensaje de {app}: {title}."
            return f"Señor, {app}: {title}."
        return f"Señor, nueva notificación: {title}."

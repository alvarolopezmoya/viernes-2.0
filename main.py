"""
main.py — Punto de entrada y orquestador principal de JARVIS 2.0.

Orden de inicialización (no modificar):
  1. asyncio WindowsSelectorEventLoopPolicy  → evita deadlocks de Edge-TTS en Windows
  2. check_env / hw_config.json              → detecta hardware, configura parámetros
  3. actions.warmup_paths()                  → pre-valida acciones en caché
  4. QApplication                            → debe existir antes de crear cualquier widget
  5. JARVISInterface                         → ventana oculta, esperando wake
  6. JARVISBrain                             → carga Whisper en memoria (operación pesada)
  7. JARVISListener                          → daemon thread de escucha pasiva
  8. app.exec()                              → loop Qt principal (bloquea hasta cerrar)
"""
import asyncio
import logging
import sys
import threading
from pathlib import Path

# ----------------------------------------------------------------
# PASO 0: asyncio policy — DEBE ir antes de cualquier import de Qt o edge-tts
# ----------------------------------------------------------------
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Silenciar logs verbosos de librerías externas
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("whisper").setLevel(logging.WARNING)
logging.getLogger("numba").setLevel(logging.WARNING)
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("fsspec").setLevel(logging.WARNING)
logging.getLogger("torio").setLevel(logging.WARNING)
logging.getLogger("comtypes").setLevel(logging.WARNING)
logger = logging.getLogger("jarvis.main")


def main() -> None:
    """Inicializa y arranca todos los módulos de JARVIS en el orden correcto."""
    print("=" * 55)
    print("V.I.E.R.N.E.S 2.0 — Iniciando sistema...")
    print("=" * 55)

    # ----------------------------------------------------------------
    # PASO 1: Validar entorno y detectar hardware
    # ----------------------------------------------------------------
    hw_config_path = Path("hw_config.json")
    if not hw_config_path.exists():
        print("[INFO] hw_config.json no encontrado. Ejecutando detección de hardware...")
        from check_env import run_all_checks
        run_all_checks()
    else:
        print(f"[OK]   hw_config.json encontrado — usando configuración existente.")
        print("       Para re-detectar hardware: python check_env.py")

    # ----------------------------------------------------------------
    # PASO 2: Pre-cargar acciones en caché + índice de aplicaciones
    # ----------------------------------------------------------------
    from actions import warmup_paths
    warmup_paths()

    # Construir índice de apps en background (no bloquea el arranque)
    import threading
    from file_manager import build_app_index
    threading.Thread(target=build_app_index, daemon=True, name="app_index_builder").start()
    logger.info("Índice de aplicaciones construyéndose en background...")

    # ----------------------------------------------------------------
    # PASO 3: Inicializar Qt Application
    # ----------------------------------------------------------------
    app = QApplication(sys.argv)
    app.setApplicationName("VIERNES")
    app.setApplicationDisplayName("VIERNES 2.0")

    # ----------------------------------------------------------------
    # PASO 4: Crear interfaz (inicia oculta)
    # ----------------------------------------------------------------
    from interface import JARVISInterface
    interface = JARVISInterface()

    # ----------------------------------------------------------------
    # PASO 5: Crear brain con callbacks thread-safe hacia Qt
    # QTimer.singleShot(0, fn) despacha la llamada al hilo principal de Qt.
    # ----------------------------------------------------------------
    from brain import JARVISBrain

    def _on_status(s: str) -> None:
        QTimer.singleShot(0, lambda: interface.set_status(s))

    def _on_message(sender: str, text: str) -> None:
        QTimer.singleShot(0, lambda: interface.display_message(sender, text))

    def _on_task(task: str) -> None:
        QTimer.singleShot(0, lambda: interface.add_task_to_panel(task))

    def _on_waveform(data: list) -> None:
        QTimer.singleShot(0, lambda: interface.update_waveform(data))

    brain = JARVISBrain(
        on_status_change=_on_status,
        on_message=_on_message,
        on_task_logged=_on_task,
        on_waveform_update=_on_waveform,
    )

    # ----------------------------------------------------------------
    # PASO 6: Crear listener con callbacks thread-safe
    # ----------------------------------------------------------------
    from listener import JARVISListener

    listener = JARVISListener()

    def _on_wake() -> None:
        """Mostrar interfaz al detectar palabra clave."""
        QTimer.singleShot(0, interface.wake_up)
        def _phrase_then_capture():
            # 1. Reproduce la frase (bloqueante — termina cuando acaba el audio)
            brain.play_wake_phrase()
            # 2. Pequeño buffer para que el micrófono no capture el eco de la frase
            time.sleep(0.15)
            # 3. Iniciar captura exactamente cuando VIERNES termina de hablar
            listener.start_capture_now()
        threading.Thread(target=_phrase_then_capture, daemon=True, name="jarvis_wake").start()

    def _on_command(audio) -> None:
        """Procesar comando de voz en un thread separado (nunca en el hilo Qt)."""
        threading.Thread(
            target=brain.process_voice,
            args=(audio,),
            daemon=True,
            name="jarvis_process",
        ).start()

    def _on_keyword_segment(audio) -> None:
        """Revisar segmento de voz en busca de palabras clave."""
        threading.Thread(
            target=brain.check_wake_keyword,
            args=(audio,),
            daemon=True,
            name="jarvis_kw_check",
        ).start()

    def _on_keyword_wake() -> None:
        """Palabra clave detectada → activar listener para capturar comando."""
        listener.trigger_wake()

    brain.set_on_keyword_wake_callback(_on_keyword_wake)
    listener.set_on_wake_callback(_on_wake)
    listener.set_on_command_ready_callback(_on_command)
    listener.set_on_keyword_segment_callback(_on_keyword_segment)

    # ----------------------------------------------------------------
    # PASO 7: Iniciar listener + hotkey global
    # ----------------------------------------------------------------
    listener.start_passive_listening()

    from config import HOTKEY_ACTIVATE
    try:
        import keyboard
        keyboard.add_hotkey(HOTKEY_ACTIVATE, listener.trigger_wake, suppress=True)
        logger.info(f"Hotkey global activo: {HOTKEY_ACTIVATE}")
    except Exception as e:
        logger.warning(f"No se pudo registrar el hotkey '{HOTKEY_ACTIVATE}': {e}")

    logger.info("VIERNES 2.0 activo. Esperando palabra clave para despertar.")
    print("\n[VIERNES] Sistema listo.")
    print("[VIERNES] Di 'Viernes' o 'papá está en casa' para activarme.")
    print(f"[VIERNES] Hotkey: {HOTKEY_ACTIVATE} para activar sin voz.")
    print("[VIERNES] Ctrl+C o cierra la ventana para detener.\n")

    # ----------------------------------------------------------------
    # PASO 8: Loop principal de Qt (bloquea hasta que el usuario cierra)
    # ----------------------------------------------------------------
    exit_code = app.exec()

    # Limpieza ordenada al salir
    listener.stop()
    logger.info("VIERNES detenido.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

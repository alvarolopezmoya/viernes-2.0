"""
listener.py — Escucha pasiva y captura de comandos de voz para JARVIS 2.0.

Activación exclusivamente por palabra clave ("jarvis", "oye jarvis", etc.).
No requiere aplausos.

Flujo:
  1. Escucha continua a 16000 Hz (nativo de Whisper, sin resampling).
  2. VAD simple: detecta segmentos de voz por energía RMS.
  3. Cada segmento de ~2.5s se envía a brain.check_wake_keyword via callback.
  4. brain.py llama trigger_wake() cuando detecta la palabra clave.
  5. listener captura el comando completo hasta silencio y lo devuelve.
"""
import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np
import pyaudio

from config import (
    CHANNELS,
    CHUNK_SIZE,
    KEYWORD_SEGMENT_SEC,
    MAX_COMMAND_DURATION_SEC,
    SAMPLE_RATE,
    SILENCE_TIMEOUT_SEC,
    VAD_ENERGY_THRESHOLD,
    WAKE_COOLDOWN_SEC,
    WAKE_COMMAND_DELAY_SEC,
    WAKE_KEYWORDS,
)

logger = logging.getLogger("jarvis.listener")

# Chunks consecutivos con energía antes de considerar inicio de voz (~128ms)
_SPEECH_ONSET_CHUNKS: int = 2

# Adaptive VAD
_VAD_MIN_THRESHOLD: float = 0.0003   # Mínimo absoluto (habitación muy silenciosa)
_VAD_MAX_THRESHOLD: float = 0.05     # Máximo absoluto (entorno muy ruidoso)
_VAD_NOISE_ALPHA: float = 0.02       # Adaptación más rápida al ruido de fondo
_VAD_TRIGGER_RATIO: float = 3.0      # Threshold = noise_floor × 3 (más sensible)


class JARVISListener:
    """
    Listener pasivo de bajo consumo activado exclusivamente por voz.
    Thread-safe: todos los métodos públicos son seguros desde cualquier thread.
    """

    def __init__(self) -> None:
        self._running: bool = False
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._stream = None

        # Callbacks
        self._on_wake_cb: Optional[Callable[[], None]] = None
        self._on_command_cb: Optional[Callable[[np.ndarray], None]] = None
        self._on_keyword_segment_cb: Optional[Callable[[np.ndarray], None]] = None

        self._listen_thread: Optional[threading.Thread] = None
        self._audio_queue: queue.Queue = queue.Queue(maxsize=300)

        # Estado interno
        self._in_cooldown: bool = False
        self._capturing_command: bool = False

        # Acumulación de voz para detección de keyword
        self._speech_chunks: list[np.ndarray] = []
        self._speech_onset_count: int = 0
        self._speech_active: bool = False
        self._speech_start_time: float = 0.0

        # Adaptive VAD — arranca con el threshold del config y se ajusta solo
        self._noise_floor: float = VAD_ENERGY_THRESHOLD / _VAD_TRIGGER_RATIO
        self._adaptive_threshold: float = VAD_ENERGY_THRESHOLD


    # ----------------------------------------------------------------
    # API Pública
    # ----------------------------------------------------------------

    def set_on_wake_callback(self, callback: Callable[[], None]) -> None:
        """Callback llamado al activar JARVIS (sin argumentos)."""
        self._on_wake_cb = callback

    def set_on_command_ready_callback(
        self, callback: Callable[[np.ndarray], None]
    ) -> None:
        """Callback llamado con el audio del comando grabado (numpy float32)."""
        self._on_command_cb = callback

    def set_on_keyword_segment_callback(
        self, callback: Callable[[np.ndarray], None]
    ) -> None:
        """
        Callback para revisión de palabras clave.
        Recibe segmentos de voz de ~2.5s. brain.py los transcribe con Whisper
        y llama a trigger_wake() si detecta la palabra clave.
        """
        self._on_keyword_segment_cb = callback

    def trigger_wake(self) -> None:
        """
        Dispara la activación desde brain.py al detectar palabra clave.
        Idempotente — no hace nada si ya hay cooldown o captura activa.
        """
        if self._in_cooldown or self._capturing_command:
            return
        logger.info("Activación por palabra clave.")
        self._trigger_wake()

    def start_capture_now(self) -> None:
        """
        Inicia la captura de comando inmediatamente (sin timer de delay).
        Llamar DESPUÉS de que la frase de activación haya terminado de sonar,
        para no capturar la voz de VIERNES como comando.
        """
        if self._capturing_command:
            return
        threading.Thread(
            target=self._capture_voice_command,
            daemon=True,
            name="jarvis_capture_now",
        ).start()

    def start_passive_listening(self) -> None:
        """Inicia el listener en un daemon thread."""
        if self._running:
            return
        self._running = True
        self._listen_thread = threading.Thread(
            target=self._listening_loop,
            daemon=True,
            name="jarvis_listener",
        )
        self._listen_thread.start()
        logger.info(
            f"Listener iniciado. Threshold={VAD_ENERGY_THRESHOLD:.3f}. "
            f"Esperando palabras clave: {WAKE_KEYWORDS[:3]}..."
        )

    def stop(self) -> None:
        """Detiene el listener y libera recursos de audio."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pyaudio:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass
        logger.info("Listener detenido.")

    # ----------------------------------------------------------------
    # Inicialización de audio
    # ----------------------------------------------------------------

    def _initialize_audio(self) -> bool:
        """Inicializa PyAudio con reintentos automáticos."""
        for attempt in range(5):
            try:
                self._pyaudio = pyaudio.PyAudio()
                self._stream = self._pyaudio.open(
                    format=pyaudio.paFloat32,
                    channels=CHANNELS,
                    rate=SAMPLE_RATE,
                    input=True,
                    frames_per_buffer=CHUNK_SIZE,
                    stream_callback=self._audio_callback,
                )
                logger.info(
                    f"Stream de audio inicializado ({SAMPLE_RATE} Hz, chunk {CHUNK_SIZE})."
                )
                return True
            except OSError as e:
                logger.warning(
                    f"Micrófono no disponible (intento {attempt + 1}/5): {e}"
                )
                time.sleep(5)
        return False

    def _audio_callback(self, in_data, frame_count, time_info, status):
        """Callback de PyAudio — nunca bloquear aquí."""
        try:
            self._audio_queue.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)

    # ----------------------------------------------------------------
    # Loop principal de escucha
    # ----------------------------------------------------------------

    def _listening_loop(self) -> None:
        """Loop principal — corre en daemon thread de bajo consumo."""
        if not self._initialize_audio():
            logger.error("No se pudo inicializar el audio. Listener terminado.")
            return

        while self._running:
            # Durante cooldown o captura de comando: NO tocar la cola.
            # _capture_voice_command corre en paralelo y necesita todos los chunks.
            # Si drenamos aquí (aunque sea para descartar), le robamos audio.
            if self._in_cooldown or self._capturing_command:
                self._reset_speech_state()
                time.sleep(0.02)
                continue

            try:
                raw = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            chunk = np.frombuffer(raw, dtype=np.float32)
            rms = float(np.sqrt(np.mean(chunk ** 2)))

            # Adaptive VAD: actualiza ruido de fondo solo en silencio
            if not self._speech_active and rms < self._adaptive_threshold:
                self._noise_floor = (
                    _VAD_NOISE_ALPHA * rms
                    + (1 - _VAD_NOISE_ALPHA) * self._noise_floor
                )
                new_threshold = self._noise_floor * _VAD_TRIGGER_RATIO
                self._adaptive_threshold = max(
                    _VAD_MIN_THRESHOLD, min(_VAD_MAX_THRESHOLD, new_threshold)
                )

            self._process_vad(chunk, rms)

    # ----------------------------------------------------------------
    # VAD y segmentación para detección de keyword
    # ----------------------------------------------------------------

    def _process_vad(self, chunk: np.ndarray, rms: float) -> None:
        """
        Detecta segmentos de voz y los envía para detección de keyword.
        Usa VAD adaptativo con threshold que se ajusta al ruido de fondo.
        """
        if rms > self._adaptive_threshold:
            self._speech_onset_count += 1

            if self._speech_active:
                self._speech_chunks.append(chunk)
                elapsed = time.time() - self._speech_start_time
                if elapsed >= KEYWORD_SEGMENT_SEC:
                    self._emit_keyword_segment()
            elif self._speech_onset_count >= _SPEECH_ONSET_CHUNKS:
                # Voz sostenida detectada — iniciar acumulación
                self._speech_active = True
                self._speech_start_time = time.time()
                self._speech_chunks = [chunk]
        else:
            if self._speech_active and self._speech_chunks:
                elapsed = time.time() - self._speech_start_time
                if elapsed >= 0.4:   # 0.4s basta para capturar "Jarvis"
                    self._emit_keyword_segment()
                else:
                    self._speech_chunks.clear()
            self._speech_active = False
            self._speech_onset_count = 0

    def _emit_keyword_segment(self) -> None:
        """Envía el segmento de voz acumulado al callback de keyword check."""
        if not self._speech_chunks or not self._on_keyword_segment_cb:
            self._reset_speech_state()
            return

        audio = np.concatenate(self._speech_chunks).astype(np.float32)
        logger.info(f"[VAD] Segmento emitido: {len(audio)} muestras ({len(audio)/16000:.2f}s)")
        self._reset_speech_state()

        threading.Thread(
            target=self._on_keyword_segment_cb,
            args=(audio,),
            daemon=True,
            name="jarvis_kw_check",
        ).start()

    def _reset_speech_state(self) -> None:
        self._speech_chunks.clear()
        self._speech_active = False
        self._speech_onset_count = 0

    # ----------------------------------------------------------------
    # Activación y captura de comando
    # ----------------------------------------------------------------

    def _trigger_wake(self) -> None:
        """Inicia la secuencia de activación: notifica UI y captura comando."""
        self._in_cooldown = True
        self._reset_speech_state()

        # Notificar UI inmediatamente
        if self._on_wake_cb:
            threading.Thread(
                target=self._on_wake_cb,
                daemon=True,
                name="jarvis_wake_cb",
            ).start()

        # Pequeña pausa para no capturar el eco de la palabra clave
        threading.Timer(WAKE_COMMAND_DELAY_SEC, self._capture_voice_command).start()

        # Levantar cooldown después del período configurado
        threading.Timer(
            WAKE_COOLDOWN_SEC,
            lambda: setattr(self, "_in_cooldown", False),
        ).start()

    def _capture_voice_command(self) -> None:
        """
        Captura el audio del comando hasta detectar silencio prolongado
        o alcanzar el tiempo máximo. Devuelve el audio al callback.

        El contador de silencio SOLO empieza después de detectar voz real.
        Antes de eso, espera hasta MAX_COMMAND_DURATION_SEC.
        """
        self._capturing_command = True
        audio_chunks: list[np.ndarray] = []
        silence_start: Optional[float] = None
        speech_detected: bool = False          # ¿Ya escuchamos voz real?
        start_time = time.time()
        # Umbral de voz: igual al adaptativo actual (calibrado para voz real)
        speech_threshold = self._adaptive_threshold
        # Umbral de silencio: más bajo que el de voz para evitar cortes en pausas naturales
        silence_threshold = self._adaptive_threshold * 0.6

        logger.info("Capturando comando de voz...")

        while self._running:
            if time.time() - start_time > MAX_COMMAND_DURATION_SEC:
                logger.info("Tiempo máximo de comando alcanzado.")
                break

            try:
                raw = self._audio_queue.get(timeout=0.03)
            except queue.Empty:
                continue

            chunk = np.frombuffer(raw, dtype=np.float32)
            audio_chunks.append(chunk)
            rms = float(np.sqrt(np.mean(chunk ** 2)))

            if rms >= speech_threshold:
                speech_detected = True
                silence_start = None
            elif speech_detected:
                # Solo contar silencio si ya habló algo
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= SILENCE_TIMEOUT_SEC:
                    logger.info("Silencio detectado — fin del comando.")
                    break
            # Si aún no ha hablado, seguimos esperando sin cortar

        self._capturing_command = False

        if audio_chunks and self._on_command_cb:
            full_audio = np.concatenate(audio_chunks).astype(np.float32)
            duration = len(full_audio) / SAMPLE_RATE
            logger.info(f"Comando capturado: {duration:.1f}s")
            threading.Thread(
                target=self._on_command_cb,
                args=(full_audio,),
                daemon=True,
                name="jarvis_command_cb",
            ).start()
        else:
            logger.warning("No se capturó audio útil en el comando.")


# ----------------------------------------------------------------
# Test standalone
# ----------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 55)
    print("JARVIS Listener — Modo Test (solo voz)")
    print(f"Palabras clave activas: {WAKE_KEYWORDS}")
    print("Di 'jarvis' para activar. Ctrl+C para salir.")
    print("=" * 55)

    listener = JARVISListener()
    listener.set_on_wake_callback(lambda: print("\n>>> ACTIVACIÓN DETECTADA <<<\n"))
    listener.set_on_command_ready_callback(
        lambda audio: print(
            f">>> Comando capturado: {len(audio)/SAMPLE_RATE:.1f}s de audio <<<"
        )
    )
    # En test no hay brain, así que trigger_wake manualmente no aplica
    # Solo verificamos que el listener detecta voz y emite segmentos
    listener.set_on_keyword_segment_callback(
        lambda audio: print(
            f"[KW] Segmento de voz: {len(audio)/SAMPLE_RATE:.1f}s "
            f"— enviar a Whisper para keyword check"
        )
    )
    listener.start_passive_listening()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDeteniendo...")
        listener.stop()
        sys.exit(0)

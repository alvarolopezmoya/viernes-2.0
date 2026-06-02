"""
tts_cloned.py — TTS con clonación de voz usando XTTS v2 (Coqui).

Uso: ClonedTTS se inicializa una vez (carga el modelo en GPU),
luego speak(text) genera y reproduce audio clonando la voz de
reference_voice.wav.

Modelo: tts_models/multilingual/multi-dataset/xtts_v2
VRAM requerida: ~2GB (compatible con GTX 1650 4GB)
"""
import io
import logging
import threading
from pathlib import Path

import pygame

logger = logging.getLogger("jarvis.tts_cloned")

REFERENCE_VOICE = Path(__file__).parent / "reference_voice.wav"
XTTS_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
TTS_LANGUAGE = "es"


class ClonedTTS:
    """
    Motor TTS con clonación de voz basado en XTTS v2.
    Thread-safe: speak() puede llamarse desde cualquier thread.
    """

    def __init__(self) -> None:
        self._tts = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        threading.Thread(target=self._load, daemon=True, name="xtts_load").start()

    def _load(self) -> None:
        """Carga el modelo XTTS v2 en GPU (operación lenta, solo una vez)."""
        try:
            import os
            from TTS.api import TTS
            import torch

            # Acepta la licencia CPML no-comercial sin prompt interactivo
            os.environ["COQUI_TOS_AGREED"] = "1"

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Cargando XTTS v2 en {device.upper()}...")
            self._tts = TTS(XTTS_MODEL).to(device)
            logger.info("XTTS v2 listo.")
        except Exception as e:
            logger.error(f"Error cargando XTTS v2: {e}")
        finally:
            self._ready.set()

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._tts is not None

    def wait_ready(self, timeout: float = 60.0) -> bool:
        """Espera hasta que el modelo esté listo. Retorna True si OK."""
        return self._ready.wait(timeout) and self._tts is not None

    def speak(self, text: str) -> None:
        """
        Genera audio con XTTS v2 clonando reference_voice.wav y lo
        reproduce con pygame.mixer. Bloquea hasta que termina la reproducción.
        """
        if not self.wait_ready(timeout=60.0):
            logger.error("XTTS v2 no disponible.")
            return

        if not REFERENCE_VOICE.exists():
            logger.error(f"Archivo de referencia no encontrado: {REFERENCE_VOICE}")
            return

        with self._lock:
            try:
                import numpy as np

                logger.debug(f"XTTS sintetizando: '{text[:60]}...'")
                wav = self._tts.tts(
                    text=text,
                    speaker_wav=str(REFERENCE_VOICE),
                    language=TTS_LANGUAGE,
                )

                # Normalizar volumen al 90% del máximo para audio audible
                audio_np = np.array(wav, dtype=np.float32)
                peak = np.max(np.abs(audio_np))
                if peak > 0:
                    audio_np = audio_np / peak * 0.90
                audio_int16 = (audio_np * 32767).astype(np.int16)
                # Duplicar canal mono → estéreo para pygame.sndarray
                audio_stereo = np.column_stack([audio_int16, audio_int16])

                # Reproducir con pygame
                sound = pygame.sndarray.make_sound(audio_stereo)
                sound.set_volume(1.0)
                channel = sound.play()
                while channel and channel.get_busy():
                    pygame.time.wait(50)

            except Exception as e:
                logger.error(f"Error en ClonedTTS.speak: {e}")

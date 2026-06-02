"""
calibrate.py — Muestra RMS del micrófono en tiempo real.
Ejecutar: py -3.11 calibrate.py
Sirve para ajustar CLAP_ENERGY_THRESHOLD en config.py.
"""
import pyaudio
import numpy as np
import time

SAMPLE_RATE = 16000
CHUNK_SIZE = 1024

p = pyaudio.PyAudio()
stream = p.open(
    format=pyaudio.paFloat32,
    channels=1,
    rate=SAMPLE_RATE,
    input=True,
    frames_per_buffer=CHUNK_SIZE,
)

print("Calibración de micrófono — Ctrl+C para salir")
print("Habla, aplaude, etc. para ver los valores RMS")
print("-" * 50)
print("RMS silencio normal: ~0.001-0.005")
print("RMS voz normal:      ~0.02-0.08")
print("RMS aplauso fuerte:  ~0.05-0.30")
print("-" * 50)

peak_rms = 0.0
try:
    while True:
        raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        chunk = np.frombuffer(raw, dtype=np.float32)
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        peak_rms = max(peak_rms, rms)

        bar_len = int(rms * 300)
        bar = "█" * min(bar_len, 50)
        print(f"\rRMS: {rms:.4f}  PEAK: {peak_rms:.4f}  |{bar:<50}|", end="", flush=True)
        time.sleep(0.01)
except KeyboardInterrupt:
    print(f"\n\nPeak RMS registrado: {peak_rms:.4f}")
    print(f"Threshold recomendado para aplausos: {peak_rms * 0.5:.4f}")
    stream.stop_stream()
    stream.close()
    p.terminate()

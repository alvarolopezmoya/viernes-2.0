# JARVIS 2.0 — Contexto del Proyecto para Claude Code

## Descripción
Asistente personal autónomo, 100% local, 100% gratuito y open-source.
Activación por doble aplauso → captura de voz → respuesta TTS < 500ms.
Personalidad: mayordomo británico sarcástico.

---

## Stack tecnológico

| Capa       | Tecnología                     | Notas                                      |
|------------|--------------------------------|--------------------------------------------|
| UI         | PyQt6                          | NO Dear PyGui — no soporta transparencias  |
| LLM        | Ollama (local)                 | Sin API key. Puerto 11434                  |
| STT        | OpenAI Whisper (local)         | Sin API key. Modelo adaptativo por HW      |
| TTS        | Edge-TTS                       | Sin API key. Voz: en-GB-RyanNeural         |
| Audio play | pygame.mixer                   | Maneja MP3 de Edge-TTS. No sounddevice     |
| Memoria    | SQLite (jarvis_memory.db)      | Local, sin servidor                        |
| Automatiz. | subprocess.Popen + pyautogui   | DETACHED_PROCESS en Windows                |

---

## OS Target
Windows 10/11 64-bit, Python 3.11+

---

## Instalación completa (en este orden)

```bash
# 1. PyTorch con CUDA para NVIDIA GTX 1650 (esta máquina):
py -3.11 -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# 2. Resto de dependencias:
py -3.11 -m pip install -r requirements.txt

# 3. Descargar modelo Ollama:
ollama pull llama3.2:1b

# 4. Verificar entorno:
python check_env.py

# 5. Iniciar JARVIS:
python main.py
```

---

## Puertos locales
- Ollama: `localhost:11434`

---

## Archivos del proyecto

| Archivo            | Responsabilidad                                          |
|--------------------|----------------------------------------------------------|
| `check_env.py`     | Detecta hardware, escribe `hw_config.json`               |
| `config.py`        | Todos los parámetros centrales. Lee `hw_config.json`     |
| `listener.py`      | Daemon thread: detección de aplausos + captura de voz    |
| `brain.py`         | STT → intención → LLM/acción → TTS                       |
| `actions.py`       | Automatización del sistema (procesos, teclado, pantalla) |
| `interface.py`     | HUD PyQt6: glassmorphism, waveform, chat, panel lateral  |
| `main.py`          | Orquestador: inicializa módulos y conecta callbacks      |
| `hw_config.json`   | Generado por `check_env.py`. No editar manualmente       |
| `jarvis_memory.db` | Creado automáticamente al iniciar. Conversaciones + prefs|

---

## Modelo de threading

| Módulo         | Thread                                       |
|----------------|----------------------------------------------|
| `listener.py`  | Daemon thread permanente (consumo < 2% CPU)  |
| `brain.py`     | Thread on-demand por comando                 |
| `interface.py` | Hilo principal de Qt (NUNCA bloquear)        |
| `actions.py`   | Subprocess desacoplado (DETACHED_PROCESS)    |
| Edge-TTS       | Thread con `asyncio.new_event_loop()` propio |

**Regla crítica:** Toda actualización de Qt desde otro thread debe usar:
```python
QTimer.singleShot(0, lambda: widget.metodo())
```

---

## Rendimiento objetivo

| Métrica                         | Target  |
|---------------------------------|---------|
| Feedback auditivo post-comando  | < 500ms |
| Transcripción Whisper base/CUDA | < 200ms |
| Primer token Ollama (GTX 1650)  | < 400ms |
| Ejecución de acción del sistema | < 300ms |

Patrón fire-and-confirm: TTS lanzado en thread paralelo ANTES de ejecutar la acción.

---

## Hardware detectado en esta máquina

- GPU: NVIDIA GeForce GTX 1650 (4GB VRAM)
- Aceleración: CUDA (NVIDIA)
- Whisper: modelo `base`, device `cuda`
- Ollama: modelo `llama3.2:1b` (por defecto, ajustable en config.py)

Para AMD/Intel GPU: descomentar `torch-directml` en `requirements.txt`.

---

## Comandos de voz reconocidos

| Comando              | Palabras clave                                     |
|----------------------|----------------------------------------------------|
| Modo trabajo         | "a trabajar", "modo trabajo"                       |
| Estado del sistema   | "estado del sistema", "cpu", "ram"                 |
| Abrir editor         | "abre vscode", "abre el editor"                    |
| Abrir terminal       | "abre terminal"                                    |
| Bloquear pantalla    | "bloquea", "bloquear pantalla"                     |
| Minimizar todo       | "minimiza todo", "escritorio limpio"               |
| Cerrar distracciones | "cierra distracciones", "modo enfoque"             |
| Captura de pantalla  | "captura de pantalla", "screenshot"                |

Cualquier otro texto va a Ollama como conversación libre.

---

## Limitaciones conocidas

- **Edge-TTS requiere conexión a internet** (genera audio en servidores de Microsoft, sin autenticación ni costo). Offline TTS alternativo: Coqui-TTS (más lento).
- **Ollama sin GPU AMD ROCm en Windows**: usar Vulkan vía `OLLAMA_GPU_DRIVER=vulkan` (automático si se detecta AMD).
- **Glassmorphism blur**: solo disponible en Windows 11 22H2+. En Windows 10 se usa fondo semitransparente sólido como fallback.

---

## Estado del proyecto

- v1.0: Todos los módulos implementados con correcciones para NVIDIA GTX 1650
- v1.1: Streaming TTS mejorado + Semantic Router implementados
  - `semantic_router.py` nuevo: TF-IDF coseno, 3 etapas, sin deps extra
  - `brain.py`: loop asyncio persistente (−15ms/chunk), ThreadPoolExecutor para TTS concurrente
  - `_detect_intent`: Stage1=keywords, Stage2=SemanticRouter, Stage3=LLM fallback

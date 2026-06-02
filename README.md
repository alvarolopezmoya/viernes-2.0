# 🎙️ V.I.E.R.N.E.S 2.0

> **Asistente de voz personal para Windows — 100% local, gratuito y open-source.**
> Wake word → Whisper (STT) → Ollama (LLM) → Edge-TTS, con personalidad de mayordomo sarcástico.

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/OS-Windows%2010%2F11-0078D6?logo=windows&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
  <img alt="Sin API keys" src="https://img.shields.io/badge/API%20keys-0-success">
  <img alt="Estado" src="https://img.shields.io/badge/estado-en%20desarrollo-orange">
</p>

Activación por palabra clave ("Viernes") → captura de voz → respuesta hablada en **< 500 ms**.
Sin servicios de pago, sin nube para la IA: Whisper y Ollama corren en tu GPU.

---

## Stack tecnológico

| Capa           | Tecnología                        | Notas                                               |
|----------------|-----------------------------------|-----------------------------------------------------|
| UI             | PyQt6                             | HUD glassmorphism, waveform animada                 |
| LLM            | Ollama (local)                    | Sin API key. Puerto 11434. Modelo: llama3.2:1b      |
| STT            | faster-whisper (local)            | Sin API key. CUDA int8_float16. Modelo: medium      |
| TTS            | Edge-TTS (Microsoft)              | Gratuito, sin autenticación. Voz: es-ES-AlvaroNeural|
| Audio playback | pygame.mixer                      | Reproduce MP3 de Edge-TTS. Streaming por frases     |
| Memoria        | SQLite (jarvis_memory.db)         | Local, sin servidor. Rotación automática            |
| Automatización | subprocess.Popen + pyautogui      | DETACHED_PROCESS en Windows                         |
| Volumen        | pycaw                             | Control exacto por porcentaje vía Windows Audio API |
| Web search     | ddgs + BeautifulSoup + wttr.in    | Resultados en tiempo real                           |

---

## Hardware objetivo (esta máquina)

| Componente | Especificación                  |
|------------|---------------------------------|
| CPU        | 6 núcleos físicos / 12 lógicos  |
| RAM        | 32 GB                           |
| GPU        | NVIDIA GeForce GTX 1650 (4 GB VRAM) |
| Aceleración| CUDA (faster-whisper + Ollama)  |
| OS         | Windows 10/11 64-bit            |
| Python     | 3.11+                           |

---

## Instalación

```bash
# 1. PyTorch con CUDA para NVIDIA GTX 1650
py -3.11 -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# 2. Resto de dependencias
py -3.11 -m pip install -r requirements.txt

# 3. Descargar modelo Ollama
ollama pull llama3.2:1b

# 4. Verificar entorno y detectar hardware
python check_env.py

# 5. Iniciar VIERNES
python main.py
```

> **Para AMD/Intel GPU:** descomentar `torch-directml` en `requirements.txt` y usar `OLLAMA_GPU_DRIVER=vulkan`.

---

## Estructura del proyecto

```
Jarvis 2.0/
├── main.py              # Orquestador principal — orden de inicialización estricto
├── brain.py             # STT → intención → LLM/acción → TTS
├── listener.py          # Daemon thread: VAD adaptativo + captura de voz
├── actions.py           # 20 acciones de automatización del sistema
├── interface.py         # HUD PyQt6: glassmorphism, waveform, chat, panel lateral
├── config.py            # Todos los parámetros centrales (leer hw_config.json)
├── check_env.py         # Detecta hardware, escribe hw_config.json
├── start_viernes.vbs    # Lanzador silencioso para arranque automático con Windows
├── hw_config.json       # Generado por check_env.py — no editar manualmente
├── jarvis_memory.db     # Creado automáticamente — conversaciones + preferencias
└── requirements.txt     # Dependencias Python
```

---

## Arquitectura de threading

| Módulo        | Thread                                          |
|---------------|-------------------------------------------------|
| `listener.py` | Daemon thread permanente (< 2% CPU en espera)   |
| `brain.py`    | Thread on-demand por cada comando               |
| `interface.py`| Hilo principal Qt — NUNCA bloquear              |
| `actions.py`  | Subprocess desacoplado (DETACHED_PROCESS)       |
| Edge-TTS      | Thread con `asyncio.new_event_loop()` propio    |
| TTS Streaming | 2 threads paralelos: generación + reproducción  |

**Regla crítica:** Toda actualización de widgets Qt desde otro thread usa:
```python
QTimer.singleShot(0, lambda: widget.metodo())
```

---

## Pipeline de procesamiento

```
Micrófono
    │
    ▼
VAD adaptativo (noise_floor EMA, ratio 3×)
    │
    ├─── Segmento corto (2.5s) ──► faster-whisper ──► ¿palabra clave?
    │                                                        │
    │                                                        ▼ sí
    ▼                                               Activa listener
Captura de comando (hasta 12s o 2.5s de silencio)
    │
    ▼
faster-whisper medium (CUDA, int8_float16, beam=1)
    │
    ▼
Detección de intención (keyword matching)
    │
    ├── Acción conocida ──► TTS feedback (paralelo) + ejecutar acción
    │
    ├── web_search ──► DuckDuckGo/wttr.in ──► Ollama resumen ──► TTS
    │
    └── Conversación libre ──► Ollama streaming ──► TTS por frases
```

---

## Palabras clave de activación

```
viernes           oye viernes        hey viernes        ey viernes
buenos días       buenas tardes      buenas noches
papá está en casa
```

---

## Comandos de voz disponibles (20 acciones)

| Acción                    | Ejemplos de frases                                              |
|---------------------------|-----------------------------------------------------------------|
| Modo trabajo              | "a trabajar", "modo trabajo", "vamos a trabajar"               |
| Estado del sistema        | "estado del sistema", "cpu", "ram", "cómo va el pc"            |
| Abrir editor              | "abre el editor", "abre vscode", "abre visual studio"          |
| Abrir terminal            | "abre la terminal", "abre consola", "abre powershell"          |
| Bloquear pantalla         | "bloquea", "bloquea el pc", "bloquear pantalla"                |
| Minimizar todo            | "minimiza todo", "escritorio limpio", "despeja la pantalla"    |
| Cerrar distracciones      | "cierra distracciones", "modo enfoque", "modo concentración"   |
| Captura de pantalla       | "captura de pantalla", "screenshot", "captura esto"            |
| Subir volumen             | "sube el volumen", "más volumen", "más alto"                   |
| Bajar volumen             | "baja el volumen", "menos volumen", "más bajo"                 |
| Volumen exacto            | "pon el volumen al 50%", "volumen al 30 por ciento"            |
| Silenciar                 | "silencia", "silencio", "mutea", "sin sonido"                  |
| Play / Pause media        | "pausa", "reanuda", "play", "para la música"                   |
| Abrir Chrome              | "abre chrome", "abre el navegador", "abre internet"            |
| Abrir Apple Music         | "abre apple music", "pon música", "quiero escuchar música"     |
| Buscar en internet        | "busca en google X", "búscame X", "googlea X"                  |
| Temporizador              | "temporizador de 5 minutos", "avísame en 30 segundos"          |
| Activar arranque automático | "activa el arranque automático", "inicia con windows"        |
| Desactivar arranque auto  | "desactiva el arranque automático", "no inicies solo"          |
| Conversación libre        | Cualquier otro texto → Ollama llama3.2:1b                      |

---

## Búsqueda web

- **Clima/tiempo** → wttr.in en tiempo real (temperatura, sensación, humedad, viento, máxima/mínima)
- **Resto** → DuckDuckGo con `timelimit="d"` (solo últimas 24h) + scraping del primer resultado con BeautifulSoup
- **Resumen** → Ollama genera 2-3 oraciones hablables con la fecha actual como contexto

---

## TTS Streaming

El sistema habla mientras Ollama sigue generando texto:

```
Ollama genera tokens
       │
       ▼
Acumulador de buffer
       │
       ├── ¿hay frase completa? (. ! ? o > 80 chars con ,)
       │           │
       │           ▼ sí
       │     Edge-TTS → bytes MP3 → cola de audio
       │
       ▼
Reproductor secuencial (thread paralelo)
   lee cola → pygame → usuario escucha
```

Primera frase audible ~500ms después de terminar de hablar.

---

## VAD Adaptativo

El listener ajusta el umbral de detección de voz dinámicamente:

```python
noise_floor = noise_floor * 0.98 + rms * 0.02   # EMA del ruido de fondo
threshold   = noise_floor * 3.0                   # Trigger = 3× el ruido base
threshold   = clamp(threshold, 0.0003, 0.05)      # Límites de seguridad
```

Esto permite funcionar bien en entornos silenciosos y ruidosos sin configuración manual.

---

## Rendimiento objetivo

| Métrica                          | Target   | Tecnología                         |
|----------------------------------|----------|------------------------------------|
| Feedback auditivo post-comando   | < 500ms  | Fire-and-confirm + TTS paralelo    |
| Transcripción Whisper (CUDA)     | < 200ms  | faster-whisper int8_float16        |
| Primer token Ollama (GTX 1650)   | < 400ms  | num_gpu=99, beam=1, ctx=1024       |
| Ejecución de acción del sistema  | < 300ms  | subprocess DETACHED_PROCESS        |
| Primera frase hablada (streaming)| ~500ms   | TTS por frases en paralelo         |

---

## Interrupción en tiempo real

Decir la palabra clave mientras VIERNES habla → para el audio inmediatamente:

```python
_stop_speaking = threading.Event()
# Al detectar keyword:
_stop_speaking.set()
pygame.mixer.music.stop()
```

---

## Arranque automático con Windows

Al activarse, VIERNES crea:
- `start_viernes.vbs` — lanzador silencioso (`pythonw.exe`, sin consola)
- Acceso directo en `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`

Para gestionar por voz:
- `"activa el arranque automático"` → crea el acceso directo
- `"desactiva el arranque automático"` → lo elimina

---

## Personalidad

- **Nombre:** VIERNES
- **Voz:** es-ES-AlvaroNeural (masculina española)
- **Estilo:** trata al usuario como "Señor", respuestas de máximo 2-3 oraciones, sarcasmo sofisticado al final de cada respuesta
- **Idioma:** español forzado en Whisper y Ollama
- **Reglas:** no inventa información, admite limitaciones con elegancia, no menciona CPU/RAM salvo que se pida

---

## Configuración central (`config.py`)

Todos los parámetros están centralizados. `hw_config.json` sobreescribe los defaults:

```python
WHISPER_MODEL         = "medium"          # tiny / base / small / medium / large
WHISPER_DEVICE        = "cuda"            # cuda / cpu
OLLAMA_MODEL          = "llama3.2:1b"     # cualquier modelo descargado en Ollama
TTS_VOICE             = "es-ES-AlvaroNeural"
SILENCE_TIMEOUT_SEC   = 2.5              # pausa para cortar el comando
MAX_COMMAND_DURATION  = 12.0             # duración máxima de un comando
VAD_ENERGY_THRESHOLD  = 0.003            # umbral base (se ajusta dinámicamente)
```

---

## Limitaciones conocidas

| Limitación | Detalle |
|------------|---------|
| Edge-TTS requiere internet | Genera audio en servidores de Microsoft (gratis, sin cuenta). Alternativa offline: Coqui-TTS (más lento) |
| Ollama sin ROCm en Windows | Para AMD usar `OLLAMA_GPU_DRIVER=vulkan` |
| Glassmorphism blur | Solo Windows 11 22H2+. En Windows 10 usa fondo semitransparente sólido |
| llama3.2:1b | Modelo pequeño — respuestas cortas y directas. Para conversaciones más ricas usar `llama3.2:3b` o `mistral` |
| VRAM 4GB | Limita el modelo Whisper a `medium` y Ollama a modelos ≤ 3b simultáneos |

---

## Puertos locales

| Servicio | Puerto         |
|----------|----------------|
| Ollama   | localhost:11434|

---

## Re-detectar hardware

```bash
python check_env.py
```

Sobreescribe `hw_config.json` con los parámetros óptimos para el hardware actual.

---

## Privacidad

VIERNES es **local-first**. Tus conversaciones y datos no salen del equipo, con una sola excepción:

| Dato | Dónde se procesa |
|------|------------------|
| Voz → texto (Whisper) | 100% local (tu GPU) |
| Conversación (Ollama) | 100% local (tu GPU) |
| Memoria / historial | Local en `jarvis_memory.db` (SQLite) |
| Texto → voz (Edge-TTS) | Servidores de Microsoft (texto de la respuesta, sin cuenta ni autenticación) |
| Búsqueda web | DuckDuckGo / wttr.in solo cuando pides buscar algo |

Los archivos con datos personales (`jarvis_memory.db`, `hw_config.json`, muestras de voz)
están en `.gitignore` y **nunca se suben al repositorio**.

---

## Aviso de seguridad

VIERNES puede ejecutar acciones sobre tu sistema (abrir apps, controlar volumen, cerrar procesos,
y —en módulos en integración— gestionar archivos). Úsalo bajo tu responsabilidad. Revisa el código
de `actions.py` y `file_manager.py` antes de concederle control total del sistema de archivos.

---

## Contribuir

Las contribuciones son bienvenidas. Para cambios grandes, abre primero un *issue* para discutir
la propuesta.

1. Haz *fork* del repositorio
2. Crea una rama: `git checkout -b feature/mi-mejora`
3. Ejecuta `python check_env.py` y verifica que `python main.py` arranca
4. Haz *commit* y abre un *Pull Request*

Áreas con mayor necesidad (ver roadmap):
- Integrar `file_manager`, `music_controller`, `reminder_manager`, `notification_watcher`
- Reemplazar el wake-word por un motor dedicado (openWakeWord / Porcupine)
- Tests unitarios (parsers de tiempo, extracción de parámetros, router semántico)

---

## Licencia

Distribuido bajo licencia **MIT**. Consulta [`LICENSE`](LICENSE) para más detalles.

---

## Disclaimer

Proyecto personal de aprendizaje, sin relación con Marvel, *Iron Man* ni Apple.
El nombre "VIERNES" (Friday) es un homenaje cariñoso, no una marca.

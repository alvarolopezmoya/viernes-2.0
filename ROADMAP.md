# 🗺️ Roadmap — VIERNES 2.0

Estado de cada subsistema y plan de evolución.

## ✅ Implementado y funcionando

- Wake word + VAD adaptativo (EMA de ruido de fondo)
- STT con faster-whisper (CUDA, int8_float16)
- 20 acciones del sistema (volumen, apps, web, timers, captura…)
- TTS streaming con Edge-TTS (habla mientras Ollama genera)
- Interrupción mid-speech (di "Viernes" para callarlo)
- Búsqueda web en tiempo real (wttr.in + DuckDuckGo)
- HUD PyQt6 con glassmorphism, waveform y panel de estadísticas
- Arranque automático con Windows

## 🔧 Escrito pero pendiente de integrar

Estos módulos están completos en el repo pero aún no conectados al pipeline de `brain.py`:

| Módulo | Qué aporta |
|--------|-----------|
| `file_manager.py` | Abrir cualquier app/archivo, leer/editar/mover/borrar por voz |
| `music_controller.py` | Siguiente/anterior, "qué suena", reproducir por género/canción/playlist |
| `reminder_manager.py` | Recordatorios con hora exacta y lenguaje natural, persistentes |
| `notification_watcher.py` | Leer notificaciones de Windows en voz alta |
| `semantic_router.py` | Stage 2 de detección de intención (TF-IDF coseno) |

## 🎯 Próximos pasos priorizados

### 1. Integrar los módulos escritos (alto valor)
- Añadir intents y extracción de parámetros en `brain.py`
- Cablear `file_manager` con **confirmación por voz** para acciones destructivas
- Conectar `semantic_router` como fallback entre keywords y conversación libre

### 2. Wake-word dedicado (alto valor de rendimiento)
- Sustituir la transcripción continua con Whisper `medium` por **openWakeWord** o **Porcupine**
- Libera VRAM y reduce falsas activaciones; Whisper queda solo para el comando real

### 3. Calidad y robustez
- `PRAGMA journal_mode=WAL` en SQLite (varias conexiones concurrentes)
- Tests unitarios: `parse_reminder_time`, `_extract_percent`, `_split_sentence`, `SemanticRouter.route`
- Limpieza de naming interno (JARVIS → VIERNES) y código muerto

### 4. Ideas a futuro
- Leer portapapeles ("traduce/corrige esto")
- Hotkey global de activación manual
- Modo "no molestar" temporal
- Clonación de voz offline con XTTS v2 (ya hay base en `tts_cloned.py`)

---

¿Quieres ayudar? Mira la sección **Contribuir** del [README](README.md).

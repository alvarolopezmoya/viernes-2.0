"""
check_env.py — Validador de entorno y detector de hardware para JARVIS.
Ejecutar antes de cualquier otra cosa: python check_env.py

Genera hw_config.json con la configuración óptima detectada automáticamente.
"""
import sys
import os
import subprocess
import json
from pathlib import Path

import psutil


def check_python_version() -> None:
    """Verifica que la versión de Python sea 3.11+."""
    if sys.version_info < (3, 11):
        print(f"[ERROR] Python 3.11+ requerido. Tienes: {sys.version}")
        sys.exit(1)
    print(f"[OK]    Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def check_ollama() -> bool:
    """Verifica que Ollama esté corriendo y lista los modelos instalados."""
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434", timeout=3)
        print("[OK]    Ollama corriendo en localhost:11434")

        # Verificar modelos disponibles
        try:
            import ollama
            models = ollama.list()
            model_names = [m.model for m in models.models] if hasattr(models, 'models') else []
            if model_names:
                print(f"[OK]    Modelos Ollama: {', '.join(model_names)}")
            else:
                print("[WARN]  Ollama no tiene modelos instalados.")
                print("        Solución: ollama pull llama3.2:1b")
        except Exception:
            print("[WARN]  No se pudo listar modelos de Ollama.")
            print("        Solución: ollama pull llama3.2:1b")

        return True
    except Exception:
        print("[ERROR] Ollama no detectado.")
        print("        Solución: Descarga Ollama en https://ollama.com y ejecútalo.")
        print("        Luego corre: ollama pull llama3.2:1b")
        return False


def check_microphone() -> bool:
    """Verifica que haya al menos un micrófono disponible."""
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        if p.get_device_count() == 0:
            print("[ERROR] No se detectó micrófono.")
            p.terminate()
            return False
        default = p.get_default_input_device_info()
        print(f"[OK]    Micrófono: {default['name']}")
        p.terminate()
        return True
    except Exception as e:
        print(f"[ERROR] PyAudio: {e}")
        print("        Solución: pip install PyAudio==0.2.14")
        return False


def detect_code_editor() -> str:
    """
    Detecta el editor de código instalado en orden de preferencia.
    Retorna la ruta ejecutable del primer editor encontrado.
    """
    candidates = [
        # VS Code
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe",
        # Cursor
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cursor" / "Cursor.exe",
        # VS Code (instalación de sistema)
        Path("C:/Program Files/Microsoft VS Code/Code.exe"),
    ]

    # Intentar via PATH primero
    for cmd in ["code", "cursor"]:
        try:
            result = subprocess.run(
                ["where", cmd], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                path = result.stdout.strip().split('\n')[0].strip()
                if path:
                    print(f"[OK]    Editor de código: {path}")
                    return path
        except Exception:
            pass

    # Buscar en rutas conocidas
    for candidate in candidates:
        if candidate.exists():
            print(f"[OK]    Editor de código: {candidate}")
            return str(candidate)

    # Fallback a Notepad
    print("[WARN]  No se encontró VS Code ni Cursor. Usando notepad.exe como fallback.")
    print("        Para instalar VS Code: https://code.visualstudio.com/")
    return "notepad.exe"


def detect_hardware() -> dict:
    """Detecta CPU, RAM y GPU disponibles en Windows."""
    hw = {
        "cpu_cores_physical": psutil.cpu_count(logical=False) or 1,
        "cpu_cores_logical": psutil.cpu_count(logical=True) or 2,
        "ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
        "gpu_vendor": None,
        "gpu_name": None,
        "gpu_vram_gb": None,
        "directml_available": False,
        "opencl_available": False,
        "cuda_available": False,
        "cuda_version": None,
    }

    # Detectar GPU via WMIC
    try:
        result = subprocess.run(
            ["wmic", "path", "win32_VideoController",
             "get", "name,AdapterRAM", "/format:csv"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            if ',' in line and 'Node' not in line and line.strip():
                parts = line.strip().split(',')
                if len(parts) >= 3:
                    vram_bytes = parts[1].strip()
                    gpu_name = parts[2].strip()
                    if gpu_name and gpu_name != "Name":
                        hw["gpu_name"] = gpu_name
                        if vram_bytes.isdigit():
                            hw["gpu_vram_gb"] = round(int(vram_bytes) / (1024 ** 3), 1)
                        if "AMD" in gpu_name or "Radeon" in gpu_name:
                            hw["gpu_vendor"] = "amd"
                        elif "NVIDIA" in gpu_name or "GeForce" in gpu_name or "RTX" in gpu_name or "GTX" in gpu_name:
                            hw["gpu_vendor"] = "nvidia"
                        elif "Intel" in gpu_name:
                            hw["gpu_vendor"] = "intel"
                        break
    except Exception:
        pass

    # Verificar CUDA via nvidia-smi (no requiere torch)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            hw["cuda_available"] = True
            hw["gpu_vendor"] = "nvidia"
            # Parsear nombre y VRAM desde nvidia-smi (más fiable que WMIC)
            line = result.stdout.strip().split('\n')[0]
            parts = [p.strip() for p in line.split(',')]
            if parts:
                hw["gpu_name"] = parts[0]
            if len(parts) >= 2:
                # parts[1] es algo como "4096 MiB"
                vram_str = parts[1].replace("MiB", "").replace("GiB", "").strip()
                try:
                    if "GiB" in parts[1]:
                        hw["gpu_vram_gb"] = round(float(vram_str), 1)
                    else:
                        hw["gpu_vram_gb"] = round(int(vram_str) / 1024, 1)
                except (ValueError, TypeError):
                    pass
            print(f"[OK]    NVIDIA GPU detectada: {hw['gpu_name']} ({hw.get('gpu_vram_gb', '?')} GB VRAM)")
        else:
            # Intentar via torch si está instalado
            try:
                import torch
                if torch.cuda.is_available():
                    hw["cuda_available"] = True
                    hw["gpu_vendor"] = "nvidia"
            except ImportError:
                pass
    except FileNotFoundError:
        # nvidia-smi no disponible, intentar torch
        try:
            import torch
            if torch.cuda.is_available():
                hw["cuda_available"] = True
                hw["gpu_vendor"] = "nvidia"
        except ImportError:
            pass
    except Exception:
        pass

    # Verificar DirectML (AMD/Intel en Windows)
    try:
        import torch_directml
        hw["directml_available"] = True
    except ImportError:
        pass

    # Verificar OpenCL
    try:
        import pyopencl
        hw["opencl_available"] = True
    except ImportError:
        pass

    return hw


def configure_for_hardware(hw: dict) -> dict:
    """Genera la configuración óptima de JARVIS según el hardware detectado."""
    config = {}

    # --- Whisper STT ---
    if hw["cuda_available"]:
        config["whisper_device"] = "cuda"
        config["whisper_model"] = "base"
        print("[HW]    Whisper: NVIDIA CUDA → modelo 'base'")
    elif hw["directml_available"] and hw["gpu_vendor"] in ("amd", "intel"):
        config["whisper_device"] = "directml"
        config["whisper_model"] = "base"
        print(f"[HW]    Whisper: {(hw['gpu_vendor'] or 'GPU').upper()} DirectML → modelo 'base'")
    else:
        config["whisper_device"] = "cpu"
        config["whisper_model"] = "tiny"
        print("[HW]    Whisper: CPU → modelo 'tiny'")

    config["whisper_threads"] = hw["cpu_cores_physical"]

    # --- Ollama LLM ---
    vram = hw.get("gpu_vram_gb") or 0
    ram = hw["ram_gb"]

    if ram >= 16 and vram >= 6:
        config["ollama_model"] = "llama3.1:8b"
    elif ram >= 8:
        config["ollama_model"] = "llama3.2:3b"
    else:
        config["ollama_model"] = "llama3.2:1b"

    config["ollama_num_thread"] = hw["cpu_cores_physical"]
    print(
        f"[HW]    Ollama: {config['ollama_model']} "
        f"(RAM: {ram}GB, VRAM: {vram}GB)"
    )

    # --- Audio workers ---
    config["audio_workers"] = max(2, hw["cpu_cores_logical"] // 2)

    return config


def configure_ollama_env(hw: dict) -> None:
    """Configura variables de entorno para Ollama según GPU."""
    envs = {
        "OLLAMA_NUM_THREAD": str(hw["cpu_cores_physical"]),
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_CONTEXT_LENGTH": "2048",
        "OLLAMA_MAX_LOADED_MODELS": "1",
    }
    if hw["gpu_vendor"] == "amd":
        envs["OLLAMA_GPU_DRIVER"] = "vulkan"
        envs["OLLAMA_GPU_LAYERS"] = "-1" if (hw.get("gpu_vram_gb") or 0) >= 4 else "0"
        print("[HW]    Ollama: AMD GPU via Vulkan activado")
    elif hw["gpu_vendor"] == "nvidia":
        envs["OLLAMA_GPU_LAYERS"] = "-1"
        print("[HW]    Ollama: NVIDIA CUDA activado")

    for k, v in envs.items():
        os.environ[k] = v


def configure_cpu_parallelism(hw: dict) -> None:
    """Fuerza NumPy/SciPy a usar todos los cores físicos disponibles."""
    cores = str(hw["cpu_cores_physical"])
    os.environ["OMP_NUM_THREADS"] = cores
    os.environ["OPENBLAS_NUM_THREADS"] = cores
    os.environ["MKL_NUM_THREADS"] = cores
    os.environ["NUMEXPR_NUM_THREADS"] = cores


def run_all_checks() -> dict:
    """Ejecuta todas las verificaciones y retorna la configuración detectada."""
    print("=" * 55)
    print("JARVIS 2.0 — Verificación de Entorno")
    print("=" * 55)

    check_python_version()

    hw = detect_hardware()
    print(
        f"\n[HW]    CPU: {hw['cpu_cores_physical']} cores físicos / "
        f"{hw['cpu_cores_logical']} lógicos"
    )
    print(f"[HW]    RAM: {hw['ram_gb']} GB")

    if hw["gpu_name"]:
        print(f"[HW]    GPU: {hw['gpu_name']} ({hw.get('gpu_vram_gb', '?')} GB VRAM)")
        print(f"[HW]    CUDA:     {'SÍ' if hw['cuda_available'] else 'NO'}")
        print(f"[HW]    DirectML: {'SÍ' if hw['directml_available'] else 'NO'}")
        print(f"[HW]    OpenCL:   {'SÍ' if hw['opencl_available'] else 'NO'}")
    else:
        print("[HW]    GPU: No detectada o sin información")

    print()
    ollama_ok = check_ollama()
    print()
    mic_ok = check_microphone()

    print()
    code_editor = detect_code_editor()

    print()
    hw_config = configure_for_hardware(hw)
    configure_ollama_env(hw)
    configure_cpu_parallelism(hw)

    # Combinar todo en un único JSON
    full_config = {**hw, **hw_config, "code_editor": code_editor}

    config_path = Path("hw_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(full_config, f, indent=2, ensure_ascii=False)

    print(f"\n[OK]    Configuración guardada en {config_path.resolve()}")

    print()
    if not ollama_ok or not mic_ok:
        print("[WARN]  Hay problemas de entorno. Corrige antes de iniciar JARVIS.")
    else:
        print("[READY] Entorno validado. Puedes iniciar: python main.py")

    return full_config


if __name__ == "__main__":
    run_all_checks()

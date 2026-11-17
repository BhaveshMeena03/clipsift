import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import IS_WINDOWS

log = logging.getLogger(__name__)
_dll_done = False


def setup_cuda_dlls() -> list[str]:
    """On Windows, make the pip-installed cuBLAS/cuDNN DLLs visible before ctranslate2 loads."""
    global _dll_done
    added = []
    if not IS_WINDOWS or _dll_done:
        return added
    _dll_done = True
    for sp in {Path(p) for p in sys.path if p.endswith("site-packages")}:
        nv = sp / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in nv.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(bin_dir))
            except OSError:
                pass
    return added


def nvidia_smi() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe and IS_WINDOWS:
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(cand) if cand.is_file() else None
    if not exe:
        return []
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        r = subprocess.run([exe, "--query-gpu=name,memory.total,memory.free,driver_version",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append({"name": parts[0], "total_mb": int(float(parts[1])), "free_mb": int(float(parts[2])),
                             "driver": parts[3] if len(parts) > 3 else ""})
            except ValueError:
                continue
    return gpus


def cuda_device_count() -> int:
    setup_cuda_dlls()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception as e:
        log.info("ctranslate2 CUDA check failed: %s", e)
        return 0


def system_info() -> dict:
    import psutil
    vm = psutil.virtual_memory()
    return {
        "gpus": nvidia_smi(),
        "cuda_devices": cuda_device_count(),
        "ram_total_mb": vm.total // 2**20,
        "ram_free_mb": vm.available // 2**20,
        "cpu_cores": psutil.cpu_count(logical=False) or os.cpu_count(),
        "cpu_threads": os.cpu_count(),
        "platform": sys.platform,
    }

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import IS_WINDOWS
from .util import find_on_path

log = logging.getLogger(__name__)
_dll_done = False
# CUDA's default order is "fastest first"; nvidia-smi lists by PCI bus. Make them agree so the
# card we measure is the card we use on multi-GPU PCs.
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")


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
    exe = find_on_path("nvidia-smi")
    if not exe and IS_WINDOWS:
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(cand) if cand.is_file() else None
    if not exe:
        return []
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        r = subprocess.run([exe, "--query-gpu=index,name,memory.total,memory.free,driver_version",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                gpus.append({"index": int(parts[0]), "name": parts[1], "total_mb": int(float(parts[2])),
                             "free_mb": int(float(parts[3])), "driver": parts[4] if len(parts) > 4 else ""})
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


def cuda_compute_types(device_index: int = 0) -> set[str]:
    """What the speech runtime can do on this card, e.g. no float16 on GTX 10-series."""
    setup_cuda_dlls()
    try:
        import ctranslate2
        return set(ctranslate2.get_supported_compute_types("cuda", device_index))
    except Exception as e:
        log.info("could not read CUDA compute types: %s", e)
        return set()


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

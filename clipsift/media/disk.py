import shutil
from pathlib import Path

from ..util import human_bytes

MARGIN = 2 * 1024**3  # always leave 2 GB


class DiskSpaceError(RuntimeError):
    pass


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def ensure_space(path: Path, needed: int, what: str) -> None:
    free = free_bytes(path)
    if free < needed + MARGIN:
        raise DiskSpaceError(f"Not enough disk space for {what}: needs about {human_bytes(needed)} plus "
                             f"{human_bytes(MARGIN)} spare, but only {human_bytes(free)} is free on {Path(path).anchor or path}.")


def estimate_export_bytes(duration_s: float, video_kbps: int = 30000, audio_kbps: int = 256) -> int:
    return int(duration_s * (video_kbps + audio_kbps) * 1000 / 8 * 1.15)

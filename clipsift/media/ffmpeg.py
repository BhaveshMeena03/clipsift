import logging
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path

from ..config import IS_WINDOWS
from ..util import Cancelled

log = logging.getLogger(__name__)

_overrides: dict[str, str] = {}
_tools_dir: Path | None = None


class FFmpegError(RuntimeError):
    pass


def configure(tools_dir: Path | None = None, ffmpeg: str = "", ffprobe: str = "") -> None:
    global _tools_dir
    _tools_dir = Path(tools_dir) if tools_dir else None
    _overrides.clear()
    if ffmpeg:
        _overrides["ffmpeg"] = ffmpeg
    if ffprobe:
        _overrides["ffprobe"] = ffprobe
    _encoder_cache.clear()


def _which(name: str) -> str:
    if name in _overrides and Path(_overrides[name]).is_file():
        return _overrides[name]
    exe = name + (".exe" if IS_WINDOWS else "")
    if _tools_dir:
        for cand in (_tools_dir / "ffmpeg" / "bin" / exe, _tools_dir / "ffmpeg" / exe, _tools_dir / exe):
            if cand.is_file():
                return str(cand)
    found = shutil.which(name)
    if not found:
        raise FFmpegError(f"{name} was not found. Run the setup script or set its path in Settings.")
    return found


def ffmpeg_bin() -> str:
    return _which("ffmpeg")


def ffprobe_bin() -> str:
    return _which("ffprobe")


def popen_kwargs() -> dict:
    kw = {}
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def kill_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def run_ffmpeg(args: list[str], *, duration: float | None = None, on_progress=None, cancel=None,
               cwd: Path | None = None, overwrite: bool = False) -> None:
    """Run ffmpeg with -progress parsing. Raises Cancelled or FFmpegError.

    Default is -n so ffmpeg itself refuses to replace an existing file.
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y" if overwrite else "-n",
           "-progress", "pipe:1", "-nostats", "-loglevel", "error", *args]
    log.debug("ffmpeg %s", " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            cwd=str(cwd) if cwd else None, **popen_kwargs())
    err_tail: deque[str] = deque(maxlen=40)

    def read_err():
        for line in iter(proc.stderr.readline, b""):
            err_tail.append(line.decode("utf-8", errors="replace").rstrip())

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    try:
        for raw in iter(proc.stdout.readline, b""):
            if cancel and cancel():
                kill_proc(proc)
                raise Cancelled()
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if on_progress and duration:
                    on_progress(min(1.0, max(0.0, us / 1e6 / duration)))
        proc.wait()
    except BaseException:
        kill_proc(proc)
        raise
    finally:
        t.join(timeout=5)
    if proc.returncode != 0:
        msg = "\n".join(err_tail) or f"ffmpeg exited with {proc.returncode}"
        raise FFmpegError(msg[-2000:])


def run_capture(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs())


_encoder_cache: dict[str, bool] = {}


def nvenc_available() -> bool:
    """Listed is not enough: a 1-frame test encode proves the GPU and driver work."""
    if "nvenc" in _encoder_cache:
        return _encoder_cache["nvenc"]
    ok = False
    try:
        out = run_capture([ffmpeg_bin(), "-hide_banner", "-encoders"], timeout=20).stdout.decode("utf-8", "replace")
        if "h264_nvenc" in out:
            r = run_capture([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                             "color=c=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", "h264_nvenc",
                             "-f", "null", os.devnull], timeout=30)
            ok = r.returncode == 0
    except Exception as e:
        log.info("nvenc check failed: %s", e)
    _encoder_cache["nvenc"] = ok
    return ok

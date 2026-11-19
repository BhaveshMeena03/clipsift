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
    global _listed
    _encoder_cache.clear()
    _listed = None


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


# Hardware H.264 encoders, best first. Each has a max-quality and a plain variant; the plain one
# covers older cards and drivers that reject options like lookahead or B-frames.
HW_ENCODERS = {
    "nvenc": {"label": "NVIDIA NVENC", "codec": "h264_nvenc", "pix_fmt": "yuv420p", "variants": [
        ["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "{q}", "-b:v", "0", "-maxrate", "40M", "-bufsize", "80M",
         "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32", "-bf", "3", "-profile:v", "high"],
        ["-preset", "p4", "-rc", "vbr", "-cq", "{q}", "-b:v", "0", "-profile:v", "high"]]},
    "amf": {"label": "AMD AMF", "codec": "h264_amf", "pix_fmt": "yuv420p", "variants": [
        ["-quality", "quality", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q2}", "-profile:v", "high"],
        ["-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q2}"]]},
    "qsv": {"label": "Intel Quick Sync", "codec": "h264_qsv", "pix_fmt": "nv12", "variants": [
        ["-preset", "veryslow", "-global_quality", "{q}", "-profile:v", "high"],
        ["-global_quality", "{q}"]]},
}
_encoder_cache: dict[str, bool] = {}
_listed: set[str] | None = None


def _listed_encoders() -> set[str]:
    global _listed
    if _listed is None:
        try:
            out = run_capture([ffmpeg_bin(), "-hide_banner", "-encoders"], timeout=20).stdout.decode("utf-8", "replace")
            _listed = {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 1 and ln.startswith(" V")}
        except Exception as e:
            log.info("could not list encoders: %s", e)
            _listed = set()
    return _listed


def encoder_args(name: str, variant: int, fast: bool = False) -> list[str]:
    enc = HW_ENCODERS[name]
    q, q2 = ("21", "23") if fast else ("17", "19")
    args = [a.replace("{q}", q).replace("{q2}", q2) for a in enc["variants"][variant]]
    return ["-c:v", enc["codec"], *args, "-pix_fmt", enc["pix_fmt"]]


def _test_encode(args: list[str]) -> bool:
    """Listed is not enough: a short 1080x1920 encode with the real options proves card, driver and options."""
    try:
        r = run_capture([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                         "color=c=black:s=1080x1920:r=30:d=0.2", "-frames:v", "3", *args, "-f", "null", os.devnull],
                        timeout=45)
        return r.returncode == 0
    except Exception as e:
        log.info("test encode failed: %s", e)
        return False


def pick_hw_encoder(prefer: str = "auto", fast: bool = False) -> tuple[str, int] | None:
    """(encoder, variant) that works on this PC, or None for the CPU encoder."""
    if prefer == "x264":
        return None
    order = list(HW_ENCODERS) if prefer == "auto" else [prefer] if prefer in HW_ENCODERS else []
    listed = _listed_encoders()
    for name in order:
        if HW_ENCODERS[name]["codec"] not in listed:
            continue
        for v in range(len(HW_ENCODERS[name]["variants"])):
            key = f"{name}:{v}:{fast}"
            if key not in _encoder_cache:
                _encoder_cache[key] = _test_encode(encoder_args(name, v, fast))
                log.info("encoder %s variant %s: %s", name, v, "works" if _encoder_cache[key] else "not usable")
            if _encoder_cache[key]:
                return name, v
    return None


def mark_encoder_bad(name: str, variant: int) -> None:
    """A real export failed with it; don't use it again this session."""
    for fast in (False, True):
        _encoder_cache[f"{name}:{variant}:{fast}"] = False


def hw_encoder_label() -> str | None:
    pick = pick_hw_encoder("auto")
    return HW_ENCODERS[pick[0]]["label"] if pick else None


def nvenc_available() -> bool:
    pick = pick_hw_encoder("nvenc")
    return pick is not None

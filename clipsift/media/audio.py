import subprocess
import threading
from collections import deque
from pathlib import Path

import numpy as np

from ..util import Cancelled
from .ffmpeg import FFmpegError, ffmpeg_bin, kill_proc, popen_kwargs

SR = 16000


def _map_args(tracks: list[int] | None, sr: int) -> list[str]:
    tracks = sorted(set(int(t) for t in (tracks or [0])))
    # async resampling keeps sample count tied to timestamps when the recording has audio gaps
    tail = f"aresample={sr}:async=1000,aformat=sample_fmts=s16:channel_layouts=mono"
    if len(tracks) == 1:
        return ["-map", f"0:a:{tracks[0]}", "-af", tail]
    ins = "".join(f"[0:a:{t}]" for t in tracks)
    return ["-filter_complex", f"{ins}amix=inputs={len(tracks)}:normalize=0:dropout_transition=0,{tail}[a]",
            "-map", "[a]"]


def _read_exact(stream, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            break
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def stream_pcm(path: Path, tracks: list[int] | None = None, sr: int = SR, start: float = 0.0,
               duration: float | None = None, block_s: float = 60.0, cancel=None):
    """Yield (t0, float32 mono block) without loading the whole recording."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-sn", "-dn", *_map_args(tracks, sr), "-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            bufsize=1 << 20, **popen_kwargs())
    err: deque[str] = deque(maxlen=20)
    th = threading.Thread(target=lambda: [err.append(x.decode("utf-8", "replace").rstrip())
                                          for x in iter(proc.stderr.readline, b"")], daemon=True)
    th.start()
    block_bytes = int(block_s * sr) * 2
    n_done = 0
    try:
        while True:
            if cancel and cancel():
                raise Cancelled()
            buf = _read_exact(proc.stdout, block_bytes)
            if not buf:
                break
            buf = buf[: len(buf) // 2 * 2]
            arr = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            yield start + n_done / sr, arr
            n_done += len(arr)
        proc.wait(timeout=60)
        if proc.returncode != 0:
            raise FFmpegError("audio decode failed: " + " | ".join(err))
    finally:
        kill_proc(proc)
        th.join(timeout=2)


def load_audio(path: Path, start: float, duration: float, tracks: list[int] | None = None, sr: int = SR,
               cancel=None) -> np.ndarray:
    parts = [a for _t, a in stream_pcm(path, tracks, sr, max(0.0, start), duration, block_s=120, cancel=cancel)]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    out = np.concatenate(parts)
    return out[: int(round(duration * sr))]

import logging
from pathlib import Path

import av

from ..util import Cancelled
from .ffmpeg import ffmpeg_bin, run_capture

log = logging.getLogger(__name__)
# None keeps libav's own no-op log hook. Any numeric level installs a Python callback
# that needs the GIL from decoder threads, which can deadlock frame-threaded decoding.
av.logging.set_level(None)


def iter_frames(path: Path, *, fps: float | None = None, keyframes_only: bool = False, start: float = 0.0,
                end: float | None = None, cancel=None):
    """Yield (t, av.VideoFrame) with t in seconds from file start, same base as ffmpeg -ss.

    fps=None with keyframes_only gives every keyframe. Decoding is lazy, nothing is buffered.
    """
    container = av.open(str(path), options={"probesize": "20M"})
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        if keyframes_only:
            stream.codec_context.skip_frame = "NONKEY"
        tb = stream.time_base
        fmt_start = (container.start_time or 0) / 1_000_000
        if start > 0:
            container.seek(int((start + fmt_start) / tb), stream=stream, backward=True, any_frame=False)
        step = (1.0 / fps) if fps else 0.0
        next_t = start
        n = 0
        for frame in container.decode(stream):
            n += 1
            if cancel and n % 30 == 0 and cancel():
                raise Cancelled()
            if frame.pts is None:
                continue
            t = float(frame.pts * tb) - fmt_start
            if t < start - 1e-3:
                continue
            if end is not None and t > end:
                break
            if step:
                if t + 1e-6 < next_t:
                    continue
                while next_t <= t + 1e-6:
                    next_t += step
            yield t, frame
    finally:
        container.close()


def gray_small(frame, w: int, h: int):
    return frame.to_ndarray(format="gray", width=w, height=h)


def grab_jpeg(path: Path, t: float, max_w: int = 1280) -> bytes:
    r = run_capture([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
                     "-frames:v", "1", "-vf", f"scale='min({max_w},iw)':-2", "-q:v", "3", "-f", "image2pipe",
                     "-vcodec", "mjpeg", "pipe:1"], timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("Could not grab a frame at that time.")
    return r.stdout

import os
from pathlib import Path

from ..media.ffmpeg import run_ffmpeg


def audio_map(tracks: list[int], label: str = "aout") -> tuple[list[str], str | None]:
    """filter_complex part and map target for the chosen audio tracks."""
    if not tracks:
        return [], None
    if len(tracks) == 1:
        return [], f"0:a:{tracks[0]}"
    ins = "".join(f"[0:a:{t}]" for t in tracks)
    return [f"{ins}amix=inputs={len(tracks)}:normalize=0:dropout_transition=0[{label}]"], f"[{label}]"


def make_proxy(src: Path, start: float, end: float, out: Path, tracks: list[int], cancel=None) -> Path:
    """Small horizontal preview with a margin, used for quick look and trimming."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    dur = max(0.5, end - start)
    graph, amap = audio_map(tracks)
    args = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
    vf = "scale=-2:360:flags=bilinear,fps=30,format=yuv420p"
    if graph:
        args += ["-filter_complex", ";".join(graph)]
    args += ["-map", "0:v:0", "-vf", vf]
    if amap:
        args += ["-map", amap, "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-movflags", "+faststart", str(tmp)]
    run_ffmpeg(args, duration=dur, cancel=cancel, overwrite=True)
    os.replace(tmp, out)
    return out

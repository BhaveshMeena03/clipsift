import json
import statistics
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path

from .ffmpeg import ffprobe_bin, run_capture


class ProbeError(RuntimeError):
    pass


@dataclass
class AudioTrack:
    index: int          # position among audio streams (0:a:N)
    codec: str
    channels: int
    sample_rate: int
    title: str = ""
    language: str = ""


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int           # display size, after rotation and pixel aspect
    height: int
    fps: float
    vcodec: str
    rotation: int = 0
    audio: list[AudioTrack] = field(default_factory=list)
    creation_time: str = ""
    size_bytes: int = 0

    @property
    def orientation(self) -> str:
        return "vertical" if self.height > self.width * 1.05 else "horizontal"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["orientation"] = self.orientation
        return d


def _fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den = float(den)
        return float(num) / den if den else 0.0
    except (ValueError, AttributeError):
        return 0.0


def probe(path: Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"File not found: {path}")
    r = run_capture([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                    timeout=120)
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError(f"Could not read this file as video: {msg[-1] if msg else 'unknown error'}")
    data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]
    if not vids:
        raise ProbeError("No video stream in this file (audio-only or cover art only).")
    v = vids[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if not w or not h:
        raise ProbeError("Video stream has no size; the file may be damaged.")
    sar = v.get("sample_aspect_ratio") or "1:1"
    try:
        sn, sd = (int(x) for x in sar.split(":"))
        if sn > 0 and sd > 0 and sn != sd:
            w = int(round(w * sn / sd))
    except ValueError:
        pass
    rotation = 0
    for sd_item in v.get("side_data_list") or []:
        if "rotation" in sd_item:
            try:
                rotation = int(float(sd_item["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int((v.get("tags") or {}).get("rotate", 0))
        except ValueError:
            rotation = 0
    if abs(rotation) % 180 == 90:
        w, h = h, w
    dur = 0.0
    for cand in (fmt.get("duration"), v.get("duration")):
        try:
            dur = float(cand)
            if dur > 0:
                break
        except (TypeError, ValueError):
            continue
    if dur <= 0:
        raise ProbeError("Could not find the duration; the file may still be recording or is damaged.")
    fps = _fps(v.get("avg_frame_rate", "")) or _fps(v.get("r_frame_rate", ""))
    if fps <= 0 or fps > 240:
        fps = 30.0
    audio = []
    for i, s in enumerate(x for x in streams if x.get("codec_type") == "audio"):
        tags = s.get("tags") or {}
        audio.append(AudioTrack(index=i, codec=s.get("codec_name", ""), channels=int(s.get("channels") or 0),
                                sample_rate=int(s.get("sample_rate") or 0), title=tags.get("title", "") or tags.get("handler_name", ""),
                                language=tags.get("language", "")))
    return MediaInfo(path=str(path), duration=dur, width=w, height=h, fps=round(fps, 3),
                     vcodec=v.get("codec_name", ""), rotation=rotation, audio=audio,
                     creation_time=(fmt.get("tags") or {}).get("creation_time", ""),
                     size_bytes=int(fmt.get("size") or path.stat().st_size))


def keyframe_interval(path: Path, sample_s: float = 120.0, start: float = 0.0) -> float | None:
    """Median seconds between keyframes over a sample. None if unknown."""
    r = run_capture([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                     "-read_intervals", f"{start}%+{sample_s}", "-show_entries", "frame=pts_time,best_effort_timestamp_time",
                     "-of", "json", str(path)], timeout=180)
    if r.returncode != 0:
        return None
    try:
        frames = json.loads(r.stdout or b"{}").get("frames", [])
    except json.JSONDecodeError:
        return None
    times = []
    for f in frames:
        t = f.get("pts_time") or f.get("best_effort_timestamp_time")
        try:
            times.append(float(t))
        except (TypeError, ValueError):
            continue
    times = sorted(set(times))
    if len(times) < 3:
        return None
    gaps = [b - a for a, b in pairwise(times) if b > a]
    return statistics.median(gaps) if gaps else None

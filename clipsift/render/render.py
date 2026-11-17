"""Build and run the ffmpeg graph for one 9:16 clip."""
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..media.ffmpeg import FFmpegError, ffmpeg_bin, nvenc_available, popen_kwargs, run_ffmpeg
from ..media.probe import probe
from . import overlay

log = logging.getLogger(__name__)

FULL = (1080, 1920)
PREVIEW = (540, 960)


class RenderError(RuntimeError):
    pass


@dataclass
class RenderSpec:
    src: Path
    width: int                 # source display size
    height: int
    fps: float
    orientation: str
    start: float
    end: float
    layout: dict = field(default_factory=dict)
    lines: list = field(default_factory=list)   # clip-relative, with "display" text
    hook: str = ""
    hook_seconds: float = 0.0
    mute_spans: list = field(default_factory=list)  # clip-relative
    export_tracks: list = field(default_factory=list)
    mute_tracks: list = field(default_factory=list)
    caption_y: float = 0.70
    hook_y: float | None = None
    highlight: bool = False
    loudnorm: bool = True
    encoder: str = "auto"
    quality: str = "max"
    preview: bool = False

    @property
    def duration(self) -> float:
        return max(0.1, self.end - self.start)


def even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def _rect_px(rect, w: int, h: int) -> tuple[int, int, int, int]:
    x, y, rw, rh = (float(v) for v in rect)
    cw, ch = even(rw * w), even(rh * h)
    cw, ch = min(cw, w - w % 2), min(ch, h - h % 2)
    cx = min(max(0, int(round(x * w))), w - cw)
    cy = min(max(0, int(round(y * h))), h - ch)
    return cw, ch, cx, cy


def visible_rect(layout: dict, src_w: int, src_h: int, size=FULL) -> tuple[float, float, float, float]:
    """Part of the source frame (normalized x0, y0, x1, y1) that ends up in the gameplay panel."""
    mode = layout.get("gameplay_mode", "crop")
    r = layout.get("gameplay") if mode != "full" else None
    if not r:
        return (0.0, 0.0, 1.0, 1.0)
    x, y, w, h = (float(v) for v in r)
    if mode == "fit":
        return (x, y, x + w, y + h)
    W, H = size
    fc_h = H * float(layout.get("facecam_h", 0.30)) if layout.get("facecam") else 0
    br_h = H * float(layout.get("brand_h", 0.085)) if layout.get("brand_on", True) else 0
    panel = W / max(1.0, H - fc_h - br_h)
    a = (w * src_w) / max(1.0, h * src_h)
    pan = max(-1.0, min(1.0, float(layout.get("pan", 0.0))))
    if a > panel:
        vw = w * panel / a
        x0 = x + (w - vw) / 2 * (1 + pan)
        return (x0, y, x0 + vw, y + h)
    vh = h * a / panel
    y0 = y + (h - vh) / 2
    return (x, y0, x + w, y0 + vh)


def inside_fraction(rect, vis) -> float:
    x, y, w, h = rect
    ix = max(0.0, min(x + w, vis[2]) - max(x, vis[0]))
    iy = max(0.0, min(y + h, vis[3]) - max(y, vis[1]))
    return (ix * iy) / max(1e-9, w * h)


def panel_sizes(spec: RenderSpec, size) -> tuple[int, int, int]:
    W, H = size
    lay = spec.layout
    fc_h = even(H * float(lay.get("facecam_h", 0.30))) if lay.get("facecam") else 0
    br_h = even(H * float(lay.get("brand_h", 0.085))) if lay.get("brand_on", True) else 0
    gp_h = H - fc_h - br_h
    if gp_h < H * 0.3:
        raise RenderError("Facecam and branding panels leave too little room for gameplay.")
    return fc_h, gp_h, br_h


def video_graph(spec: RenderSpec, size, has_base: bool, has_text: bool) -> tuple[str, dict]:
    W, H = size
    fps = spec.fps
    parts = []
    info = {}
    src_in = f"[0:v]setpts=PTS-STARTPTS,fps={fps:.3f}"
    if spec.orientation == "vertical":
        src_aspect = spec.width / spec.height
        if abs(src_aspect - W / H) < 0.01:
            parts.append(f"{src_in},scale={W}:{H}:flags=lanczos,setsar=1[comp]")
        else:
            parts.append(f"{src_in},split=2[vb][vf]")
            parts.append(f"[vb]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=24,"
                         f"eq=brightness=-0.15[bg]")
            parts.append(f"[vf]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg]")
            parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[comp]")
        info["panels"] = None
    else:
        lay = spec.layout
        fc_h, gp_h, br_h = panel_sizes(spec, size)
        info["panels"] = (fc_h, gp_h, br_h)
        insets = [i for i in (lay.get("hud_insets") or []) if i.get("src")]
        n = 1 + (1 if fc_h else 0) + len(insets)
        labels = [f"s{i}" for i in range(n)]
        parts.append(f"{src_in},split={n}" + "".join(f"[{x}]" for x in labels) if n > 1 else f"{src_in}[s0]")
        mode = lay.get("gameplay_mode", "crop")
        pan = max(-1.0, min(1.0, float(lay.get("pan", 0.0))))
        if mode == "full" or not lay.get("gameplay"):
            gw, gh, gx, gy = even(spec.width), even(spec.height), 0, 0
            gw, gh = min(gw, spec.width - spec.width % 2), min(gh, spec.height - spec.height % 2)
        else:
            gw, gh, gx, gy = _rect_px(lay["gameplay"], spec.width, spec.height)
        g = f"[s0]crop={gw}:{gh}:{gx}:{gy}"
        if mode in ("fit", "full"):
            parts.append(f"{g},split=2[gb][gf]")
            parts.append(f"[gb]scale={W}:{gp_h}:force_original_aspect_ratio=increase,crop={W}:{gp_h},"
                         f"gblur=sigma=22,eq=brightness=-0.18[gbg]")
            parts.append(f"[gf]scale={W}:{gp_h}:force_original_aspect_ratio=decrease:flags=lanczos[gfg]")
            parts.append("[gbg][gfg]overlay=(W-w)/2:(H-h)/2,setsar=1[gp]")
        else:
            parts.append(f"{g},scale={W}:{gp_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                         f"crop={W}:{gp_h}:(iw-ow)/2*(1+{pan:.3f}):(ih-oh)/2,setsar=1[gp]")
        cur = "[gp]"
        for k, ins in enumerate(insets):
            iw_, ih_, ix, iy = _rect_px(ins["src"], spec.width, spec.height)
            scale = float(ins.get("scale", 0.32))
            tw = even(W * scale)
            parts.append(f"[{labels[1 + (1 if fc_h else 0) + k]}]crop={iw_}:{ih_}:{ix}:{iy},scale={tw}:-2[ins{k}]")
            pos = ins.get("pos", "tr")
            mx = even(W * 0.02)
            x = {"l": f"{mx}", "r": f"W-w-{mx}", "c": "(W-w)/2"}[pos[1:2] or "r"]
            y = f"{mx}" if pos.startswith("t") else f"H-h-{mx}"
            parts.append(f"{cur}[ins{k}]overlay={x}:{y}[gpi{k}]")
            cur = f"[gpi{k}]"
        if fc_h:
            fw, fh, fx, fy = _rect_px(lay["facecam"], spec.width, spec.height)
            parts.append(f"[s1]crop={fw}:{fh}:{fx}:{fy},scale={W}:{fc_h}:force_original_aspect_ratio=increase:"
                         f"flags=lanczos,crop={W}:{fc_h},setsar=1[fc]")
        base = "[1:v]" if has_base else None
        if base:
            parts.append(f"{base}format=yuv420p,setsar=1[canvas]")
            nxt = "[canvas]"
        else:
            parts.append(f"color=c=0x0B0C0F:s={W}x{H}:r={fps:.3f}:d={spec.duration:.3f}[canvas]")
            nxt = "[canvas]"
        if fc_h:
            parts.append(f"{nxt}[fc]overlay=0:0[c1]")
            nxt = "[c1]"
        parts.append(f"{nxt}{cur}overlay=0:{fc_h}:shortest=1,setsar=1[comp]")
    if has_text:
        ti = 2 if has_base else 1
        parts.append(f"[{ti}:v]format=rgba[txt]")
        parts.append("[comp][txt]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[vout]")
    else:
        parts.append("[comp]format=yuv420p[vout]")
    return ";".join(parts), info


def audio_graph(spec: RenderSpec, loud: dict | None = None, for_measure: bool = False) -> str | None:
    tracks = spec.export_tracks
    if not tracks:
        return None
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spec.mute_spans)
    parts = []
    mute_set = set(spec.mute_tracks)
    separate = bool(expr) and mute_set and mute_set != set(tracks)
    labels = []
    for i, t in enumerate(tracks):
        chain = f"[0:a:{t}]asetpts=PTS-STARTPTS,aresample=48000"
        if separate and t in mute_set:
            chain += f",volume=volume=0:enable='{expr}'"
        parts.append(f"{chain}[a{i}]")
        labels.append(f"[a{i}]")
    if len(labels) > 1:
        parts.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[am]")
    else:
        parts.append(f"{labels[0]}anull[am]")
    post = "[am]"
    chain = []
    if expr and not separate:
        chain.append(f"volume=volume=0:enable='{expr}'")
    d = spec.duration
    chain.append("afade=t=in:st=0:d=0.04")
    chain.append(f"afade=t=out:st={max(0.0, d - 0.06):.3f}:d=0.06")
    if for_measure:
        chain.append("loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json")
    elif loud:
        chain.append("loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={input_i}:measured_TP={input_tp}:"
                     "measured_LRA={input_lra}:measured_thresh={input_thresh}:offset={target_offset}:linear=true"
                     .format(**loud))
        chain.append("aresample=48000")
    parts.append(post + ",".join(chain) + "[aout]")
    return ";".join(parts)


def measure_loudness(spec: RenderSpec) -> dict | None:
    g = audio_graph(spec, for_measure=True)
    if not g:
        return None
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-ss", f"{spec.start:.3f}", "-i", str(spec.src),
           "-t", f"{spec.duration:.3f}", "-filter_complex", g, "-map", "[aout]", "-vn", "-f", "null", os.devnull]
    r = subprocess.run(cmd, capture_output=True, timeout=600, stdin=subprocess.DEVNULL, **popen_kwargs())
    text = r.stderr.decode("utf-8", "replace")
    m = re.search(r"\{\s*\"input_i\".*?\}", text, re.S)
    if r.returncode != 0 or not m:
        log.warning("loudness measure failed: %s", text[-400:])
        return None
    vals = json.loads(m.group(0))
    try:
        if float(vals["input_i"]) < -70 or vals["input_i"] in ("-inf", "inf"):
            return None
    except (KeyError, ValueError):
        return None
    return vals


def video_codec_args(spec: RenderSpec) -> list[str]:
    if spec.preview:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
    use_nv = spec.encoder == "nvenc" or (spec.encoder == "auto" and nvenc_available())
    best = spec.quality != "fast"
    if use_nv:
        return ["-c:v", "h264_nvenc", "-preset", "p7" if best else "p4", "-tune", "hq", "-rc", "vbr",
                "-cq", "17" if best else "21", "-b:v", "0", "-maxrate", "40M" if best else "20M", "-bufsize", "80M",
                "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32", "-bf", "3",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "slow" if best else "veryfast", "-crf", "16" if best else "20",
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def render(spec: RenderSpec, out: Path, work: Path, *, cancel=None, on_progress=None, overwrite: bool = False) -> dict:
    size = PREVIEW if spec.preview else FULL
    W, H = size
    if not spec.preview:
        spec.fps = min(60.0, spec.fps or 30.0)
    else:
        spec.fps = min(30.0, spec.fps or 30.0)
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("t*.png"):
        old.unlink()
    has_base = spec.orientation == "horizontal"
    fc_h = gp_h = br_h = 0
    if has_base:
        fc_h, gp_h, br_h = panel_sizes(spec, size)
        brand = spec.layout.get("brand") or {}
        overlay.base_canvas(size, brand if spec.layout.get("brand_on", True) else None, br_h, work / "base.png")
    cap_y = spec.caption_y
    if has_base:
        lo, hi = (fc_h + gp_h * 0.12) / H, (fc_h + gp_h * 0.92) / H
        cap_y = min(max(cap_y, lo), hi)
    hook_y = spec.hook_y
    if hook_y is None:
        hook_y = (fc_h / H) if (has_base and fc_h) else 0.14
    has_text = bool(spec.lines) or bool(spec.hook)
    if has_text:
        tl = overlay.text_timeline(size, spec.lines, spec.hook, hook_y, cap_y, spec.duration, work,
                                   hook_seconds=spec.hook_seconds, highlight=spec.highlight)
    vg, _info = video_graph(spec, size, has_base, has_text)
    loud = measure_loudness(spec) if (spec.loudnorm and not spec.preview and spec.export_tracks) else None
    ag = audio_graph(spec, loud)
    args = ["-ss", f"{spec.start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.src)]
    if has_base:
        args += ["-loop", "1", "-framerate", f"{spec.fps:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(work / "base.png")]
    if has_text:
        args += ["-f", "concat", "-safe", "0", "-i", str(tl)]
    graph = vg + (";" + ag if ag else "")
    args += ["-filter_complex", graph, "-map", "[vout]"]
    if ag:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k" if spec.preview else "256k", "-ar", "48000"]
    else:
        args += ["-an"]
    args += video_codec_args(spec)
    args += ["-r", f"{spec.fps:.3f}", "-t", f"{spec.duration:.3f}", "-movflags", "+faststart"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("." + out.stem + ".part.mp4")
    if tmp.exists():
        tmp.unlink()
    try:
        run_ffmpeg([*args, str(tmp)], duration=spec.duration, on_progress=on_progress, cancel=cancel, overwrite=True)
    except FFmpegError as e:
        if "nvenc" in " ".join(video_codec_args(spec)) and not spec.preview:
            log.warning("NVENC failed (%s), retrying with x264", str(e)[:200])
            spec.encoder = "x264"
            args = [a for a in args]
            i = args.index("-c:v")
            j = args.index("-r")
            args = args[:i] + video_codec_args(spec) + args[j:]
            run_ffmpeg([*args, str(tmp)], duration=spec.duration, on_progress=on_progress, cancel=cancel, overwrite=True)
        else:
            raise
    info = check_output(tmp, size, spec.duration, bool(ag))
    if out.exists() and not overwrite:
        raise RenderError(f"{out} already exists")
    os.replace(tmp, out)
    return info


def check_output(path: Path, size, duration: float, want_audio: bool) -> dict:
    info = probe(path)
    problems = []
    if (info.width, info.height) != tuple(size):
        problems.append(f"size {info.width}x{info.height}, expected {size[0]}x{size[1]}")
    if abs(info.duration - duration) > 0.35:
        problems.append(f"duration {info.duration:.2f}s, expected {duration:.2f}s")
    if want_audio and not info.audio:
        problems.append("no audio track")
    if problems:
        raise RenderError("Output check failed: " + "; ".join(problems))
    return info.to_dict()


def cleanup(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)

"""Preview and export jobs: candidate + edits + layout -> RenderSpec -> mp4."""
import logging
import time
from pathlib import Path

from ..media import tracks
from ..media.disk import ensure_space, estimate_export_bytes
from ..render.render import RenderSpec, render
from ..text.captions import build_lines, display_text, word_times_for_line
from ..text.profanity import Profanity, mute_spans
from ..util import fmt_ts, read_json, safe_filename, stable_hash, unique_path

log = logging.getLogger(__name__)

DEFAULT_LAYOUT = {"facecam": None, "gameplay": None, "gameplay_mode": "full", "facecam_h": 0.30, "brand_h": 0.085,
                  "brand_on": True, "brand": {"text": "fortysevenfrags", "color": "#111318", "text_color": "#FFFFFF"},
                  "hud_insets": []}


def clip_bounds(cand: dict) -> tuple[float, float]:
    e = cand.get("edit") or {}
    s = float(e["start"]) if e.get("start") is not None else float(cand["start"])
    t = float(e["end"]) if e.get("end") is not None else float(cand["end"])
    return s, t


def caption_words(paths, cand: dict, start: float, end: float) -> tuple[list[dict], dict]:
    cap = cand.get("captions") or {}
    win = cap.get("window") or [cand["start"], cand["end"]]
    if start >= win[0] and end <= win[1]:
        segs = {k: (v or {}).get("status") for k, v in (cap.get("segs") or {}).items()}
        return cap.get("words", []), segs
    tr = read_json(paths.source_work(cand["source_id"]) / "transcript" / "transcript.json") or {}
    words = [w for w in tr.get("words", []) if start - 1 <= w["start"] <= end + 1]
    segs = {s["id"]: s.get("status") for s in tr.get("segments", [])}
    return words, segs


def candidate_lines(paths, cand: dict, settings: dict) -> dict:
    """Caption lines for the current trim, with what to display and what to mute."""
    e = cand.get("edit") or {}
    start, end = clip_bounds(cand)
    words, seg_status = caption_words(paths, cand, start, end)
    lines = build_lines(words, start, end, seg_status=seg_status, max_words=int(settings.get("caption_words_max") or 4),
                        overrides=e.get("caption_overrides") or [])
    prof = Profanity(settings.get("profanity_extra"), settings.get("profanity_allow"))
    censor = e.get("censor_on", settings.get("censor_on", True))
    upper = bool(settings.get("caption_upper"))
    spans_words = []
    estimated = False
    for ln in lines:
        ln["display"] = display_text(ln["text"], prof=prof, censor=censor, upper=upper)
        wt = word_times_for_line(ln)
        ln["word_times"] = [{"text": w["text"], "start": round(w["start"] - start, 3), "end": round(w["end"] - start, 3)}
                            for w in wt]
        if not ln["hidden"]:
            if any(w["estimated"] for w in wt) and any(prof.is_profane(w["text"]) for w in wt):
                estimated = True
            spans_words += [{"text": w["text"], "start": w["start"], "end": w["end"]} for w in wt]
    spans = []
    if censor:
        for a, b in mute_spans(spans_words, prof):
            a, b = max(0.0, a - start), min(end - start, b - start)
            if b > a:
                spans.append((round(a, 3), round(b, 3)))
    warnings = {"flagged": sum(1 for ln in lines if ln["status"] == "flagged" and not ln["hidden"]),
                "hidden_unclear": sum(1 for ln in lines if ln["hidden"]),
                "estimated_mute": estimated}
    return {"lines": lines, "mute_spans": spans, "warnings": warnings, "start": start, "end": end}


def build_spec(db, paths, settings: dict, cand_id: int, preview: bool) -> tuple[RenderSpec, dict, dict]:
    cand = db.one("SELECT * FROM candidates WHERE id=?", (cand_id,))
    if not cand:
        raise RuntimeError("Candidate not found.")
    src = db.one("SELECT * FROM sources WHERE id=?", (cand["source_id"],))
    if not src or not src.get("path") or not Path(src["path"]).is_file():
        raise RuntimeError("The source recording is missing.")
    info = src.get("info") or {}
    e = cand.get("edit") or {}
    layout = dict(DEFAULT_LAYOUT)
    layout["brand"] = dict(DEFAULT_LAYOUT["brand"], text=settings.get("creator_name") or "")
    if src.get("layout_id"):
        row = db.one("SELECT * FROM layouts WHERE id=?", (src["layout_id"],))
        if row:
            layout.update(row["data"] or {})
    for k in ("gameplay_mode", "pan", "facecam_h"):
        if e.get(k) is not None:
            layout[k] = e[k]
    if e.get("facecam_off"):
        layout["facecam"] = None
    vertical_src = (src.get("orientation") or info.get("orientation")) == "vertical"
    if not vertical_src and e.get("auto_insets", True):
        layout["hud_insets"] = list(layout.get("hud_insets") or []) + auto_insets(
            cand, layout, src, int(info.get("width") or 1920), int(info.get("height") or 1080))
    cl = candidate_lines(paths, cand, settings)
    captions_on = e.get("captions_on", settings.get("captions_on", True))
    lines = [ln for ln in cl["lines"] if not ln["hidden"]] if captions_on else []
    tp = tracks.plan_tracks(src.get("track_roles") or {}, len(info.get("audio") or []))
    vertical = (src.get("orientation") or info.get("orientation")) == "vertical"
    hook = (e.get("hook") or "").strip()
    if hook and e.get("censor_on", settings.get("censor_on", True)):
        hook = Profanity(settings.get("profanity_extra"), settings.get("profanity_allow")).mask_text(hook)
    spec = RenderSpec(
        src=Path(src["path"]), width=int(info.get("width") or src.get("width") or 1920),
        height=int(info.get("height") or src.get("height") or 1080), fps=float(info.get("fps") or src.get("fps") or 30),
        orientation="vertical" if vertical else "horizontal", start=cl["start"], end=cl["end"], layout=layout,
        lines=lines, hook=hook, hook_seconds=float(e.get("hook_seconds") or settings.get("hook_seconds") or 0),
        mute_spans=cl["mute_spans"], export_tracks=tp["export"], mute_tracks=tp["mute"],
        caption_y=float(e.get("caption_y") or (settings.get("vertical_caption_y") if vertical else settings.get("caption_y"))),
        hook_y=(float(settings.get("vertical_hook_y")) if vertical else None),
        highlight=bool(settings.get("caption_highlight")), loudnorm=bool(settings.get("loudnorm", True)),
        encoder=settings.get("encoder", "auto"), quality=settings.get("export_quality", "max"), preview=preview)
    return spec, cand, src


INSET_STYLE = {"killfeed": ("tr", 0.40), "banner": ("tc", 0.55), "reward": ("bc", 0.40)}


def auto_insets(cand: dict, layout: dict, src: dict, w: int, h: int) -> list[dict]:
    """HUD regions that proved this clip but would be cropped away get overlaid as insets."""
    from ..analysis.visual_scan import hud_config
    from ..render.render import inside_fraction, visible_rect
    regions = hud_config(src.get("game") or "generic", layout)
    vis = visible_rect(layout, w, h)
    have = [tuple(i.get("src") or ()) for i in layout.get("hud_insets") or []]
    out = []
    used = {e.get("region") for e in cand.get("evidence") or [] if e.get("region")}
    for key in ("killfeed", "banner", "reward"):
        rect = regions.get(key)
        if key not in used or not rect or tuple(rect) in have:
            continue
        if inside_fraction(rect, vis) < 0.85:
            pos, scale = INSET_STYLE[key]
            banner = regions.get("banner")
            if key == "killfeed" and "banner" in used and banner and inside_fraction(banner, vis) >= 0.85:
                pos = "br"  # the banner is already on screen at the top; don't cover it
            out.append({"src": list(rect), "pos": pos, "scale": scale, "auto": True, "region": key})
    return out


def spec_key(spec: RenderSpec) -> str:
    return stable_hash({k: v for k, v in spec.__dict__.items() if k != "src"} | {"src": str(spec.src)})


def run_preview(ctx) -> dict:
    spec, cand, _src = build_spec(ctx.db, ctx.paths, ctx.settings, ctx.job["candidate_id"], preview=True)
    key = spec_key(spec)
    out = ctx.paths.previews / f"cand_{cand['id']}_{key}.mp4"
    if not out.is_file():
        for old in ctx.paths.previews.glob(f"cand_{cand['id']}_*.mp4"):
            old.unlink(missing_ok=True)
        render(spec, out, ctx.paths.previews / f"work_{cand['id']}", cancel=ctx.cancelled,
               on_progress=lambda p: ctx.progress("render", p, "Rendering preview"), overwrite=True)
    return {"path": str(out), "key": key}


def export_dir(paths, settings) -> Path:
    d = Path(settings.get("export_dir") or paths.exports).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_export(ctx) -> dict:
    db, paths, st = ctx.db, ctx.paths, ctx.settings
    spec, cand, src = build_spec(db, paths, st, ctx.job["candidate_id"], preview=False)
    outdir = export_dir(paths, st)
    ensure_space(outdir, estimate_export_bytes(spec.duration), "this export")
    title = safe_filename(src.get("title") or Path(src["path"]).stem, 40)
    stamp = fmt_ts(spec.start).replace(":", "-")
    name = safe_filename(f"{st.get('creator_name') or 'clip'}_{title}_{stamp}_{cand['category']}") + ".mp4"
    out = unique_path(outdir / name)
    if out.exists() or Path(out).resolve() == Path(spec.src).resolve():
        raise RuntimeError("Refusing to write over an existing file or the source recording.")
    info = render(spec, out, paths.home / "render_work" / f"job_{ctx.id}", cancel=ctx.cancelled,
                  on_progress=lambda p: ctx.progress("render", p, f"Exporting {out.name}"))
    now = time.time()
    db.insert("exports", candidate_id=cand["id"], path=str(out), params_json={"start": spec.start, "end": spec.end,
              "hook": spec.hook, "lines": len(spec.lines), "muted": spec.mute_spans}, probe_json=info, created_at=now)
    db.update("candidates", cand["id"], status="exported", export_path=str(out), updated_at=now)
    from ..render.render import cleanup
    cleanup(paths.home / "render_work" / f"job_{ctx.id}")
    return {"path": str(out), "width": info["width"], "height": info["height"], "duration": info["duration"]}

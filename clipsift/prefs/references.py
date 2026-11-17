"""Reference library: your past clips, their metrics, and features read from the videos."""
import csv
import io
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..analysis.candidates import CATEGORIES, build_features, category_scores
from ..util import Cancelled

log = logging.getLogger(__name__)

COLUMN_ALIASES = {
    "permalink": ["permalink", "link", "url", "post link", "reel link", "post url"],
    "posted_at": ["publish time", "posted", "date", "post date", "created", "timestamp", "published", "upload date"],
    "views": ["views", "plays", "reel plays", "video views", "play count", "initial plays"],
    "likes": ["likes", "like count", "like_count"],
    "comments": ["comments", "comment count", "comments_count"],
    "shares": ["shares", "share count"],
    "saves": ["saves", "saved"],
    "title": ["title", "description", "caption", "name"],
    "file": ["file", "path", "video file", "filename"],
}
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M",
                "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y"]


def parse_number(v) -> int | None:
    """'1,234' -> 1234, '1.2K' -> 1200, '' -> None (missing is not zero)."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("_", "")
    if s == "" or s.lower() in ("-", "n/a", "na", "none", "null", "--"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMbB]?)", s)
    if not m:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]
    return int(round(float(m.group(1)) * mult))


def parse_date(v) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return float(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else dt.replace(tzinfo=UTC).timestamp()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


def map_columns(headers: list[str]) -> dict[str, str]:
    norm = {h: re.sub(r"\s+", " ", h.strip().lower().replace("_", " ")) for h in headers}
    out = {}
    for field, aliases in COLUMN_ALIASES.items():
        for h, n in norm.items():
            if n in aliases and field not in out:
                out[field] = h
    return out


def parse_csv(text: str) -> tuple[list[dict], dict]:
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = map_columns(reader.fieldnames or [])
    if not ({"permalink", "file", "title"} & set(cols)):
        raise ValueError("Could not find a link, file or title column. Expected headers like Permalink, Views, Likes, Publish time.")
    rows = []
    for raw in reader:
        row = {"permalink": (raw.get(cols.get("permalink", ""), "") or "").strip() or None,
               "file": (raw.get(cols.get("file", ""), "") or "").strip() or None,
               "title": (raw.get(cols.get("title", ""), "") or "").strip()[:200] or None,
               "posted_at": parse_date(raw.get(cols.get("posted_at", ""))),
               **{k: parse_number(raw.get(cols.get(k, ""))) if k in cols else None
                  for k in ("views", "likes", "comments", "shares", "saves")}}
        if row["permalink"] or row["file"] or row["title"]:
            rows.append(row)
    return rows, cols


def upsert_metrics(db, rows: list[dict], source: str) -> dict:
    created = updated = 0
    for r in rows:
        existing = None
        if r.get("permalink"):
            existing = db.one("SELECT * FROM refs WHERE permalink=?", (r["permalink"],))
        if not existing and r.get("file"):
            existing = db.one("SELECT * FROM refs WHERE path=?", (str(Path(r["file"]).expanduser()),))
        fields = {k: r[k] for k in ("views", "likes", "comments", "shares", "saves", "posted_at") if r.get(k) is not None}
        fields.update(metrics_source=source, metrics_at=time.time())
        if r.get("title"):
            fields["title"] = r["title"]
        if existing:
            db.update("refs", existing["id"], **fields)
            updated += 1
        else:
            path = str(Path(r["file"]).expanduser()) if r.get("file") else None
            db.insert("refs", path=path, permalink=r.get("permalink"), created_at=time.time(), **fields)
            created += 1
    return {"created": created, "updated": updated}


# ---------- analysing a reference video ----------

def caption_style(path: Path, duration: float, ocr, samples: int = 8) -> dict:
    """Look for burned-in captions: text boxes in the middle band of a few frames."""
    from ..media.frames import iter_frames
    if duration <= 1:
        return {"seen": False}
    ys, words, upper = [], [], []
    times = list(np.linspace(duration * 0.1, duration * 0.9, samples))
    for t in times:
        for _ft, frame in iter_frames(path, start=t, end=t + 1.0):
            rgb = frame.to_ndarray(format="rgb24")
            h, w = rgb.shape[:2]
            for it in ocr.read(rgb):
                cy = (it["y0"] + it["y1"]) / 2 / h
                cx = (it["x0"] + it["x1"]) / 2 / w
                txt = it["text"].strip()
                letters = [c for c in txt if c.isalpha()]
                if 0.15 < cy < 0.9 and 0.25 < cx < 0.75 and len(letters) >= 2:
                    ys.append(cy)
                    words.append(len(txt.split()))
                    upper.append(sum(c.isupper() for c in letters) / len(letters))
            break
    if len(ys) < 3:
        return {"seen": False}
    return {"seen": True, "y": float(np.median(ys)), "words_per_line": float(np.median(words)),
            "upper_share": float(np.mean(upper)), "boxes": len(ys)}


def analyse_reference(ctx, path: Path, models_dir) -> dict:
    from .. import models
    from ..analysis import asr
    from ..analysis.audio_scan import run_audio_scan
    from ..analysis.tagger import Tagger
    from ..analysis.visual_scan import OcrEngine
    from ..media.audio import load_audio
    from ..media.frames import iter_frames
    from ..media.probe import probe
    info = probe(path)
    feats = {"duration": round(info.duration, 2), "orientation": info.orientation, "width": info.width,
             "height": info.height, "fps": info.fps}
    tmp = ctx.paths.references / "work" / re.sub(r"[^\w]", "_", path.stem)[:60]
    tagger = Tagger(models_dir) if models.tagger_ready(models_dir) else None
    audio = run_audio_scan(path, info.duration, tmp, tagger=tagger, cancel=ctx.cancelled) if info.audio else None
    plan = asr.plan_asr(ctx.settings, models_dir)
    segs = []
    if info.audio and info.duration < 900:
        tr = asr.Transcriber(plan, models_dir, cpu_model=ctx.settings.get("asr_model_cpu", "small"))
        try:
            pcm = load_audio(path, 0, info.duration, cancel=ctx.cancelled)
            segs = tr.transcribe(pcm, offset=0.0, language_mode=ctx.settings.get("language_mode", "mixed"),
                                 cancel=ctx.cancelled)
        finally:
            tr.close()
    n_words = sum(len(s["words"]) for s in segs)
    feats["words_per_s"] = round(n_words / max(1.0, info.duration), 3)
    # cuts per minute from a 2 fps scan
    prev, cuts = None, 0
    for _t, fr in iter_frames(path, fps=2.0, cancel=ctx.cancelled):
        g = fr.to_ndarray(format="gray", width=64, height=36).astype(np.float32)
        h, _ = np.histogram(g, bins=16, range=(0, 255))
        h = h / max(1, g.size)
        if prev is not None and 0.5 * np.abs(h - prev).sum() > 0.35:
            cuts += 1
        prev = h
    feats["cuts_per_min"] = round(cuts / max(1 / 60, info.duration / 60), 2)
    if audio is not None:
        f = build_features(info.duration, audio, {"segments": [dict(s, status="ok") for s in segs]}, None, [])
        sc = category_scores(f, "generic")
        means = {c: float(np.mean(np.sort(sc[c])[-max(3, len(sc[c]) // 5):])) for c in CATEGORIES}
        feats["category"] = max(means, key=means.get)
        feats["category_scores"] = {k: round(v, 3) for k, v in means.items()}
        feats["laugh_share"] = round(float(np.mean(f["tag_laugh"] > 0.35)), 3)
    try:
        feats["captions"] = caption_style(path, info.duration, OcrEngine())
    except Exception as e:
        log.info("caption style read failed: %s", e)
        feats["captions"] = {"seen": False}
    return feats


def run_reference_analysis(ctx) -> dict:
    db = ctx.db
    ids = (ctx.job.get("payload") or {}).get("ref_ids") or [r["id"] for r in db.all(
        "SELECT id FROM refs WHERE path IS NOT NULL AND analyzed_at IS NULL")]
    done, failed = 0, []
    for k, rid in enumerate(ids):
        ctx.check()
        r = db.one("SELECT * FROM refs WHERE id=?", (rid,))
        if not r or not r.get("path") or not Path(r["path"]).is_file():
            failed.append(rid)
            continue
        ctx.progress("reference", k / max(1, len(ids)), f"Reading reference {k + 1}/{len(ids)}: {Path(r['path']).name}")
        try:
            feats = analyse_reference(ctx, Path(r["path"]), ctx.paths.models)
        except Cancelled:
            raise
        except Exception as e:
            log.warning("reference %s failed: %s", rid, e)
            failed.append(rid)
            continue
        db.update("refs", rid, features_json=feats, analyzed_at=time.time())
        done += 1
    return {"analysed": done, "failed": failed, "message": f"Analysed {done} reference clip(s)"}

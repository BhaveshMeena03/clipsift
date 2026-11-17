"""Visual pass. Cheap stats on every sampled frame; OCR of HUD regions only when they change."""
import logging
from pathlib import Path

import numpy as np

from ..detect import hud
from ..detect.games import game
from ..media.frames import iter_frames
from ..util import atomic_write_json, read_json

log = logging.getLogger(__name__)

BLOCK_S = 1800.0
SMALL = (64, 36)
MID = (160, 90)


def _px_rect(rect, w, h):
    x, y, rw, rh = rect
    x0 = int(max(0, min(w - 2, round(x * w))))
    y0 = int(max(0, min(h - 2, round(y * h))))
    x1 = int(max(x0 + 2, min(w, round((x + rw) * w))))
    y1 = int(max(y0 + 2, min(h, round((y + rh) * h))))
    return x0, y0, x1, y1


def _hist(img):
    h, _ = np.histogram(img, bins=16, range=(0, 255))
    return h.astype(np.float32) / max(1, img.size)


class OcrEngine:
    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        # no upscaling of small crops: same reads on HUD text, about 2x faster
        self._eng = RapidOCR(det_limit_type="max", det_limit_side_len=960)

    def read(self, rgb: np.ndarray) -> list[dict]:
        res, _ = self._eng(rgb, use_cls=False)
        items = []
        for box, text, score in res or []:
            if float(score) < 0.5 or not str(text).strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append({"text": str(text), "score": round(float(score), 3), "x0": min(xs), "x1": max(xs),
                          "y0": min(ys), "y1": max(ys)})
        return items


def hud_config(game_id: str, layout: dict | None) -> dict:
    """Regions for OCR: layout overrides first, then game defaults."""
    g = game(game_id)
    over = (layout or {}).get("hud_regions", {}).get(game_id, {})
    cfg = {}
    for key in ("killfeed", "banner", "reward"):
        r = over.get(key) or g.get(key)
        if r:
            cfg[key] = tuple(r)
    return cfg


def scan_range(path: Path, start: float, end: float, *, fps: float | None, keyframes_only: bool, regions: dict,
               hud_cfg: dict, ocr: "OcrEngine | None", names: list[str], game_id: str, cancel=None,
               on_progress=None) -> dict:
    out = {"t": [], "motion": [], "bright": [], "cut": [], "face": [], "gp": [], "events": []}
    prev_small = prev_hist = prev_face = prev_gp = None
    ocr_sig: dict[str, np.ndarray] = {}
    feed = hud.FeedTracker()
    banners = hud.BannerTracker()
    rewards_seen: dict[int, float] = {}
    span = max(1e-6, end - start)
    for t, frame in iter_frames(path, fps=fps, keyframes_only=keyframes_only, start=start, end=end, cancel=cancel):
        small = frame.to_ndarray(format="gray", width=SMALL[0], height=SMALL[1]).astype(np.float32)
        mid = frame.to_ndarray(format="gray", width=MID[0], height=MID[1]).astype(np.float32)
        hist = _hist(small)
        motion = float(np.mean(np.abs(small - prev_small))) if prev_small is not None else 0.0
        cut = float(0.5 * np.sum(np.abs(hist - prev_hist))) if prev_hist is not None else 0.0
        face = gp = 0.0
        if regions.get("facecam"):
            x0, y0, x1, y1 = _px_rect(regions["facecam"], MID[0], MID[1])
            crop = mid[y0:y1, x0:x1]
            if prev_face is not None and prev_face.shape == crop.shape:
                face = float(np.mean(np.abs(crop - prev_face)))
            prev_face = crop
        if regions.get("gameplay"):
            x0, y0, x1, y1 = _px_rect(regions["gameplay"], MID[0], MID[1])
            crop = mid[y0:y1, x0:x1]
            if prev_gp is not None and prev_gp.shape == crop.shape:
                gp = float(np.mean(np.abs(crop - prev_gp)))
            prev_gp = crop
        prev_small, prev_hist = small, hist
        out["t"].append(round(t, 3))
        out["motion"].append(round(motion, 2))
        out["bright"].append(round(float(small.mean()), 1))
        out["cut"].append(round(cut, 3))
        out["face"].append(round(face, 2))
        out["gp"].append(round(gp, 2))

        if ocr is not None and hud_cfg:
            rgb = None
            for key, rect in hud_cfg.items():
                # cheap change check on the 160x90 copy before paying for OCR
                sx0, sy0, sx1, sy1 = _px_rect(rect, MID[0], MID[1])
                sig_small = mid[sy0:sy1, sx0:sx1]
                x0, y0, x1, y1 = _px_rect(rect, frame.width, frame.height)
                last = ocr_sig.get(key)
                if last is not None and last.shape == sig_small.shape and float(np.mean(np.abs(sig_small - last))) < 2.0:
                    continue
                ocr_sig[key] = sig_small
                if rgb is None:
                    rgb = frame.to_ndarray(format="rgb24")
                items = ocr.read(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
                if not items:
                    continue
                if key == "killfeed":
                    for ev in feed.update(t, hud.killfeed_rows(items, names)):
                        out["events"].append({"t": round(t, 2), "src": "killfeed", **ev})
                elif key == "banner":
                    for ev in banners.update(t, hud.banner_events(items, game_id)):
                        out["events"].append({"t": round(t, 2), "src": "banner", **ev})
                elif key == "reward":
                    for amt in hud.reward_amounts(items):
                        last_t = rewards_seen.get(amt)
                        if last_t is None or t - last_t > 3.0:
                            out["events"].append({"t": round(t, 2), "src": "reward", "kind": "money", "amount": amt})
                        rewards_seen[amt] = t
        if on_progress and len(out["t"]) % 20 == 0:
            on_progress(min(1.0, (t - start) / span))
    return out


def run_visual_scan(path: Path, duration: float, out_dir: Path, *, fps, keyframes_only, regions, hud_cfg, ocr,
                    names, game_id, cancel=None, on_progress=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    n_blocks = max(1, int(np.ceil(duration / BLOCK_S)))
    merged = {"t": [], "motion": [], "bright": [], "cut": [], "face": [], "gp": [], "events": []}
    for b in range(n_blocks):
        s = b * BLOCK_S
        e = min(duration, s + BLOCK_S)
        f = out_dir / f"block_{b:04d}.json"
        data = read_json(f)
        if data is None:
            def prog(p, b=b):
                if on_progress:
                    on_progress((b + p) / n_blocks)
            data = scan_range(path, s, e - 1e-3 if e < duration else e + 1, fps=fps, keyframes_only=keyframes_only,
                              regions=regions, hud_cfg=hud_cfg, ocr=ocr, names=names, game_id=game_id,
                              cancel=cancel, on_progress=prog)
            atomic_write_json(f, data)
        for k in merged:
            merged[k].extend(data.get(k, []))
        if on_progress:
            on_progress((b + 1) / n_blocks)
    return merged

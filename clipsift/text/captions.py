"""Turn timed words into short caption lines for a clip window, and apply user edits."""
import re

LOW_PROB = 0.45
HIDE_STATUSES = {"unclear", "dropped"}


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def words_in_window(words: list[dict], t0: float, t1: float) -> list[dict]:
    out = []
    for w in words:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        mid = (s + e) / 2
        if t0 <= mid <= t1:
            out.append(w)
    return out


def build_lines(words: list[dict], clip_start: float, clip_end: float, *, seg_status: dict | None = None,
                max_words: int = 4, max_chars: int = 30, gap_s: float = 0.6, overrides: list[dict] | None = None,
                min_dur: float = 0.5, hold: float = 0.2) -> list[dict]:
    """Lines carry absolute times (abs_start/abs_end) and clip-relative ones (start/end)."""
    seg_status = seg_status or {}
    ws = words_in_window(words, clip_start, clip_end)
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for w in ws:
        text = w.get("text", "").strip()
        if not text:
            continue
        if cur:
            prev = cur[-1]
            chars = sum(len(x.get("text", "").strip()) + 1 for x in cur) + len(text)
            new = (len(cur) >= max_words or chars > max_chars or w["start"] - prev["end"] > gap_s
                   or re.search(r"[.?!]$", prev.get("text", "").strip())
                   or (w.get("seg") != prev.get("seg")
                       and seg_status.get(w.get("seg")) != seg_status.get(prev.get("seg"))))
            if new:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    lines = []
    for ch in chunks:
        status = "ok"
        statuses = {seg_status.get(w.get("seg"), "ok") for w in ch}
        if statuses & HIDE_STATUSES:
            status = "unclear"
        elif statuses & {"disputed", "revised", "flagged", "unverified"} or any(
                (w.get("prob") is not None and w["prob"] < LOW_PROB) for w in ch):
            status = "flagged"
        lines.append({
            "abs_start": ch[0]["start"], "abs_end": ch[-1]["end"],
            "text": " ".join(w.get("text", "").strip() for w in ch),
            "words": [{"text": w.get("text", "").strip(), "abs_start": w["start"], "abs_end": w["end"],
                       "prob": w.get("prob"), "low": w.get("prob") is not None and w["prob"] < LOW_PROB}
                      for w in ch],
            "status": status, "hidden": status == "unclear", "edited": False,
            "segs": sorted({w.get("seg") for w in ch if w.get("seg") is not None}),
        })

    for ov in overrides or []:
        _apply_override(lines, ov)

    # timing: keep inside the clip, no overlaps, short lines held a little longer
    lines.sort(key=lambda x: x["abs_start"])
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["abs_start"] if i + 1 < len(lines) else clip_end
        s = max(clip_start, ln["abs_start"])
        limit = min(nxt - 0.02, clip_end)
        e = max(ln["abs_end"], s + min_dur) + hold
        e = max(min(e, limit), min(s + 0.05, clip_end))
        ln["start"] = round(s - clip_start, 3)
        ln["end"] = round(e - clip_start, 3)
    return lines


def _apply_override(lines: list[dict], ov: dict) -> None:
    a0, a1 = ov.get("abs_start"), ov.get("abs_end")
    if a0 is None or a1 is None:
        return
    best, best_ov = None, 0.0
    for ln in lines:
        o = _overlap(a0, a1, ln["abs_start"], ln["abs_end"])
        span = min(a1 - a0, ln["abs_end"] - ln["abs_start"]) or 1e-6
        if o / span > 0.5 and o > best_ov:
            best, best_ov = ln, o
    if best is None:
        return
    if "text" in ov and ov["text"] is not None and ov["text"].strip() != best["text"]:
        best["text"] = ov["text"].strip()
        best["edited"] = True
    if ov.get("approved"):
        best["status"] = "ok"
        best["hidden"] = False
    if "hidden" in ov:
        best["hidden"] = bool(ov["hidden"])


def word_times_for_line(line: dict) -> list[dict]:
    """Per-word times for a line. Edited lines get times spread by length (estimated)."""
    if not line.get("edited"):
        return [{"text": w["text"], "start": w["abs_start"], "end": w["abs_end"], "estimated": False}
                for w in line["words"]]
    toks = line["text"].split()
    if not toks:
        return []
    a0, a1 = line["abs_start"], line["abs_end"]
    total = sum(len(t) + 1 for t in toks)
    out, pos = [], 0
    for t in toks:
        s = a0 + (a1 - a0) * pos / total
        pos += len(t) + 1
        e = a0 + (a1 - a0) * pos / total
        out.append({"text": t, "start": s, "end": e, "estimated": True})
    return out


def display_text(text: str, *, prof=None, censor: bool = True, upper: bool = False) -> str:
    if censor and prof is not None:
        text = prof.mask_text(text)
    return text.upper() if upper else text

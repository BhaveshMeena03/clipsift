"""Split long recordings into overlapping chunks and stitch the results back without
duplicate or missing words. Cut points sit in silences when there is one nearby."""


def _best_gap(target: float, speech: list[list[float]], search: float) -> float:
    """Middle of the longest silence within +-search of target, else target."""
    lo, hi = target - search, target + search
    best, best_len = target, 0.0
    prev_end = None
    for a, b in speech:
        if b < lo:
            prev_end = b
            continue
        g0 = prev_end if prev_end is not None else lo
        g1 = a
        s0, s1 = max(g0, lo), min(g1, hi)
        if s1 - s0 > best_len:
            best, best_len = (s0 + s1) / 2, s1 - s0
        prev_end = b
        if a > hi:
            break
    if prev_end is not None and prev_end < hi:
        s0 = max(prev_end, lo)
        if hi - s0 > best_len:
            best, best_len = (s0 + hi) / 2, hi - s0
    return best


def plan_chunks(duration: float, speech: list[list[float]] | None = None, chunk_s: float = 600.0,
                overlap_s: float = 20.0, search_s: float = 30.0) -> list[dict]:
    """Each chunk: decode [start, end]; keep words whose start is in [keep_from, keep_to)."""
    if duration <= chunk_s + overlap_s:
        return [{"i": 0, "start": 0.0, "end": duration, "keep_from": 0.0, "keep_to": duration + 1.0}]
    speech = sorted(speech or [])
    cuts = []
    t = chunk_s
    while t < duration - overlap_s:
        c = _best_gap(t, speech, search_s) if speech else t
        c = min(max(c, (cuts[-1] if cuts else 0.0) + overlap_s * 2), duration - overlap_s)
        cuts.append(round(c, 3))
        t = c + chunk_s
    bounds = [0.0, *cuts, duration]
    out = []
    half = overlap_s / 2
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        out.append({"i": i, "start": round(max(0.0, a - half), 3), "end": round(min(duration, b + half), 3),
                    "keep_from": a if i > 0 else 0.0, "keep_to": b if i < len(bounds) - 2 else duration + 1.0})
    return out


def has_speech(chunk: dict, speech: list[list[float]]) -> bool:
    return any(a < chunk["end"] and b > chunk["start"] for a, b in speech)


def stitch(chunks: list[dict], results: list[list[dict]]) -> list[dict]:
    """results[i] are segments (absolute times) for chunks[i]. Each word is kept by exactly one chunk."""
    out = []
    for ch, segs in zip(chunks, results, strict=True):
        for seg in segs:
            words = [w for w in seg.get("words", []) if ch["keep_from"] <= w["start"] < ch["keep_to"]]
            if seg.get("words"):
                if not words:
                    continue
                s = dict(seg)
                s["words"] = words
                if len(words) != len(seg["words"]):
                    s["text"] = "".join(w["text"] for w in words).strip()
                    s["start"], s["end"] = words[0]["start"], words[-1]["end"]
                    s["flags"] = sorted(set(s.get("flags", [])) | {"split_at_chunk"})
                out.append(s)
            elif ch["keep_from"] <= seg["start"] < ch["keep_to"]:
                out.append(seg)
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out

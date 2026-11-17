"""Preference suggestions from your past clips and your accept/reject choices.

Views and likes are noisy: they depend on the algorithm, timing and luck. Everything here is a
suggestion with the number of clips behind it, never a claim that something causes views."""
import math
import statistics
import time

CATEGORIES = ("skill", "funny", "scare", "other")


def _metric(ref: dict) -> tuple[float | None, str | None]:
    """log views, else log likes on its own scale. None means missing, which is not zero."""
    if ref.get("views") is not None:
        return math.log1p(max(0, int(ref["views"]))), "views"
    if ref.get("likes") is not None:
        return math.log1p(max(0, int(ref["likes"]))), "likes"
    return None, None


def performance(refs: list[dict], now: float | None = None) -> dict[int, dict]:
    """Relative performance per reference (z-score), adjusted for post age when enough dates are known."""
    now = now or time.time()
    out: dict[int, dict] = {}
    for kind in ("views", "likes"):
        pts = []
        for r in refs:
            v, k = _metric(r)
            if v is None or k != kind:
                continue
            age = (now - r["posted_at"]) / 86400 if r.get("posted_at") else None
            pts.append((r["id"], v, age))
        if not pts:
            continue
        aged = [p for p in pts if p[2] is not None and p[2] >= 0]
        resid = {}
        adjusted = False
        if len(aged) >= 5:
            xs = [math.log1p(p[2]) for p in aged]
            ys = [p[1] for p in aged]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            vx = sum((x - mx) ** 2 for x in xs)
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / vx if vx > 1e-9 else 0.0
            b = max(0.0, b)  # older posts never get penalized for being old
            for rid, y, age in pts:
                resid[rid] = y - b * math.log1p(age) if age is not None else y - b * mx
            adjusted = True
        else:
            for rid, y, _age in pts:
                resid[rid] = y
        vals = list(resid.values())
        mean = statistics.fmean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        sd = max(sd, 0.25)
        for rid, v in resid.items():
            out[rid] = {"z": (v - mean) / sd, "metric": kind, "age_adjusted": adjusted}
    for r in refs:
        if r.get("favourite"):
            out.setdefault(r["id"], {"z": 0.0, "metric": None, "age_adjusted": False})
            out[r["id"]]["z"] = max(out[r["id"]]["z"], 0.0) + 1.0
            out[r["id"]]["favourite"] = True
        if r.get("rejected"):
            out.setdefault(r["id"], {"z": 0.0, "metric": None, "age_adjusted": False})
            out[r["id"]]["z"] = min(out[r["id"]]["z"], 0.0) - 1.0
            out[r["id"]]["rejected"] = True
    return out


def _wmedian(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(w for _v, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def suggest(refs: list[dict], decisions: list[dict]) -> dict:
    perf = performance(refs)
    analysed = [r for r in refs if (r.get("features") or {}).get("duration")]
    out: dict = {"n_refs": len(refs), "n_with_metrics": sum(1 for r in refs if _metric(r)[0] is not None),
                 "n_analysed": len(analysed), "n_decisions": len(decisions), "notes": []}
    # clip length
    with_perf = [r for r in analysed if r["id"] in perf]
    if len(with_perf) >= 3:
        vals = [r["features"]["duration"] for r in with_perf]
        ws = [math.exp(max(-2.0, min(2.0, perf[r["id"]]["z"]))) for r in with_perf]
        target = _wmedian(vals, ws)
        q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [min(vals), target, max(vals)]
        out["duration"] = {"target": round(target, 1), "range": [round(q[0], 1), round(q[-1], 1)], "n": len(with_perf),
                           "basis": "your better-performing clips weigh more"}
    elif len(analysed) >= 3:
        vals = [r["features"]["duration"] for r in analysed]
        out["duration"] = {"target": round(statistics.median(vals), 1), "range": [min(vals), max(vals)],
                           "n": len(vals), "basis": "median of your references (no performance data)"}
    # moment type
    weights = {}
    detail = {}
    for cat in CATEGORIES:
        zs = [perf[r["id"]]["z"] for r in analysed if r["id"] in perf and r["features"].get("category") == cat]
        acc = [d for d in decisions if d.get("category") == cat]
        n_acc = sum(1 for d in acc if d["decision"] == "accepted")
        ref_part = sum(zs) / (len(zs) + 3) if zs else 0.0   # shrunk toward 0 with few clips
        all_acc = sum(1 for d in decisions if d["decision"] == "accepted")
        base_rate = (all_acc + 1) / (len(decisions) + 2)
        rate = (n_acc + base_rate * 4) / (len(acc) + 4)       # shrunk toward your overall rate
        dec_part = (rate - base_rate) * 2 if decisions else 0.0
        mult = max(0.75, min(1.3, 1.0 + 0.2 * ref_part + dec_part))
        weights[cat] = round(mult, 3)
        detail[cat] = {"refs": len(zs), "mean_z": round(statistics.fmean(zs), 2) if zs else None,
                       "decisions": len(acc), "accepted": n_acc}
    out["category_weights"] = weights
    out["category_detail"] = detail
    # caption style read off the references
    caps = [r["features"]["captions"] for r in analysed if (r["features"].get("captions") or {}).get("seen")]
    if len(caps) >= 2:
        out["captions"] = {"words_per_line": round(statistics.median(c["words_per_line"] for c in caps)),
                           "upper": sum(1 for c in caps if c["upper_share"] > 0.6) > len(caps) / 2,
                           "y": round(statistics.median(c["y"] for c in caps), 2), "n": len(caps),
                           "basis": "read with OCR from a few frames of each reference"}
    # pacing
    paced = [r for r in with_perf if r["features"].get("words_per_s") is not None]
    if len(paced) >= 6:
        paced.sort(key=lambda r: -perf[r["id"]]["z"])
        third = max(2, len(paced) // 3)
        top, rest = paced[:third], paced[third:]

        def avg(rs, k):
            return round(statistics.fmean(r["features"].get(k) or 0 for r in rs), 2)
        out["pacing"] = {"top_words_per_s": avg(top, "words_per_s"), "rest_words_per_s": avg(rest, "words_per_s"),
                         "top_cuts_per_min": avg(top, "cuts_per_min"), "rest_cuts_per_min": avg(rest, "cuts_per_min"),
                         "n": len(paced)}
    if out["n_with_metrics"] < 8:
        out["notes"].append("Fewer than 8 clips have views or likes, so treat these as weak hints.")
    out["notes"].append("Views and likes depend on many things outside the clip. These are patterns, not causes.")
    return out

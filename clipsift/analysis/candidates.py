"""Score every second of a recording per category, pick peaks, build clip windows, merge overlaps.

Scores are ranking estimates inside one recording. They are not probabilities of anything."""
import math
from itertools import pairwise

import numpy as np

from ..detect.games import SKILL_BANNERS, game
from ..text.lexicon import LAUGH_TOKENS, cue_hits
from ..util import fmt_ts
from .tagger import GROUP_NAMES

CATEGORIES = ("skill", "funny", "scare", "other")
CATEGORY_LABEL = {"skill": "Skill play", "funny": "Funny squad moment", "scare": "Reaction / scare",
                  "other": "Other promising moment"}
LEAD = {"skill": 12.0, "funny": 10.0, "scare": 8.0, "other": 10.0}
AFTER = {"skill": 6.0, "funny": 8.0, "scare": 8.0, "other": 6.0}


# ---------- per-second features ----------

def _sec_max(arr10: np.ndarray, n: int) -> np.ndarray:
    m = min(len(arr10) // 10, n)
    out = np.full(n, float(np.min(arr10)) if len(arr10) else 0.0, np.float32)
    if m:
        out[:m] = arr10[: m * 10].reshape(m, 10).max(axis=1)
    return out


def _rolling(arr: np.ndarray, win: int, fn) -> np.ndarray:
    if len(arr) == 0:
        return arr.copy()
    half = win // 2
    # reflect, not edge: repeated edge values make the spread ~0 and noise looks like a spike
    mode = "reflect" if len(arr) > half else "edge"
    padded = np.pad(arr, (half, half), mode=mode)
    view = np.lib.stride_tricks.sliding_window_view(padded, win)[: len(arr)]
    return fn(view, axis=1).astype(np.float32)


def robust_z(x: np.ndarray, win: int = 61, floor: float = 1.5) -> np.ndarray:
    med = _rolling(x, win, np.median)
    mad = _rolling(np.abs(x - med), win, np.median)
    return (x - med) / (1.4826 * mad + floor)


def onsets_per_second(flux10: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, np.float32)
    if len(flux10) < 3:
        return out
    med = _rolling(flux10, 101, np.median)
    mad = _rolling(np.abs(flux10 - med), 101, np.median)
    thr = med + np.maximum(4.0 * 1.4826 * mad, 0.25 * np.abs(med)) + 1e-3
    left = np.concatenate([[-np.inf], flux10[:-1]])
    right = np.concatenate([flux10[1:], [-np.inf]])
    is_peak = (flux10 > thr) & (flux10 >= left) & (flux10 >= right)
    idx = np.nonzero(is_peak)[0] // 10
    idx = idx[idx < n]
    np.add.at(out, idx, 1)
    return out


def interval_fraction(intervals: list, n: int) -> np.ndarray:
    out = np.zeros(n, np.float32)
    for a, b in intervals:
        a, b = max(0.0, a), min(float(n), b)
        if b <= a:
            continue
        i0, i1 = int(a), int(math.ceil(b))
        for i in range(i0, min(i1, n)):
            out[i] += max(0.0, min(b, i + 1) - max(a, i))
    return np.clip(out, 0, 1)


def _spread(arr: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return arr
    return _rolling(arr, 2 * radius + 1, np.max)


def _mean(arr: np.ndarray, win: int) -> np.ndarray:
    return _rolling(arr, win, np.mean)


def build_features(duration: float, audio: dict, transcript: dict, visual: dict | None, events: list[dict]) -> dict:
    n = max(1, int(math.ceil(duration)))
    f: dict[str, np.ndarray] = {}
    rms_v = _sec_max(np.asarray(audio["rms_voice"], np.float32), n)
    f["jump"] = np.clip(robust_z(rms_v) / 4.0, 0, 1)
    f["trans"] = np.clip(onsets_per_second(np.asarray(audio["flux"], np.float32), n) / 5.0, 0, 1)
    f["speech"] = interval_fraction(audio.get("speech", []), n)
    quiet_before = np.zeros(n, np.float32)
    sp_cum = np.concatenate([[0.0], np.cumsum(f["speech"])])
    rms_cum = np.concatenate([[0.0], np.cumsum(rms_v)])
    for i in range(n):
        a = max(0, i - 20)
        if i - a >= 5:
            sp = (sp_cum[i] - sp_cum[a]) / (i - a)
            lvl = (rms_cum[i] - rms_cum[a]) / (i - a)
            quiet_before[i] = 1.0 if (sp < 0.3 and lvl < np.median(rms_v) + 3) else 0.0
    f["quiet_before"] = quiet_before
    tags = audio.get("tags") or {}
    for g in GROUP_NAMES:
        arr = np.asarray(tags.get(g, []), np.float32)
        out = np.zeros(n, np.float32)
        if len(arr):
            idx = np.minimum(np.arange(n) // 2, len(arr) - 1)
            out = arr[idx]
        f["tag_" + g] = out
    f["has_tags"] = bool(audio.get("has_tags"))

    # transcript cues
    words_ps = np.zeros(n, np.float32)
    lex = {c: np.zeros(n, np.float32) for c in ("skill", "funny", "scare", "other")}
    laugh_tok = np.zeros(n, np.float32)
    cue_text: dict[int, list] = {}
    for seg in transcript.get("segments", []):
        if seg.get("status") == "dropped":
            continue
        s0, s1 = int(max(0, seg["start"])), int(min(n - 1, seg["end"]))
        for w in seg.get("words", []):
            i = int(w["start"])
            if 0 <= i < n:
                words_ps[i] += 1
        hits = cue_hits(seg["text"])
        for cat, hs in hits.items():
            lex[cat][s0:s1 + 1] = np.maximum(lex[cat][s0:s1 + 1], min(1.0, 0.6 + 0.2 * (len(hs) - 1)))
            cue_text.setdefault(s0, []).append((cat, hs[0], seg["text"], seg["start"]))
        if LAUGH_TOKENS.search(seg["text"]):
            laugh_tok[s0:s1 + 1] = 1.0
    f["words_ps"] = np.clip(_mean(words_ps, 5) / 3.0, 0, 1)
    for c in lex:
        f["lex_" + c] = _spread(lex[c], 2)
    f["laugh_tok"] = laugh_tok
    f["cues"] = cue_text

    # visual
    for k in ("motion", "cut", "face", "gp"):
        f["vis_" + k] = np.zeros(n, np.float32)
    if visual and visual.get("t"):
        t = np.asarray(visual["t"], np.float32)
        idx = np.clip(t.astype(int), 0, n - 1)
        for k in ("motion", "cut", "face", "gp"):
            arr = np.zeros(n, np.float32)
            np.maximum.at(arr, idx, np.asarray(visual[k], np.float32))
            f["vis_" + k] = arr
    face = f["vis_face"]
    if face.any():
        nz = face[face > 0]
        med, mad = float(np.median(nz)), float(np.median(np.abs(nz - np.median(nz)))) + 0.5
        f["face"] = np.clip((face - med) / (4 * 1.4826 * mad), 0, 1)
    else:
        f["face"] = np.zeros(n, np.float32)
    f["cutf"] = (f["vis_cut"] > 0.35).astype(np.float32)

    # game events
    kills = np.zeros(n, np.float32)
    banner = np.zeros(n, np.float32)
    money = np.zeros(n, np.float32)
    for ev in events:
        i = int(ev["t"])
        if not 0 <= i < n:
            continue
        if ev.get("kind") in ("kill", "kill?"):
            kills[i] += 1.0 if ev["kind"] == "kill" else 0.5
        elif ev.get("kind") in SKILL_BANNERS:
            banner[i] = max(banner[i], SKILL_BANNERS[ev["kind"]])
        elif ev.get("kind") == "money":
            money[i] += float(ev.get("amount", 0))
    kill_win = _rolling(kills, 21, np.sum)
    f["kills"] = np.clip(kill_win / 3.0, 0, 1)
    f["banner"] = _spread(banner, 4)
    mwin = _rolling(money, 17, np.sum)
    ref = np.percentile(mwin[mwin > 0], 90) if (mwin > 0).any() else 1.0
    f["money"] = np.clip(mwin / max(ref, 1.0), 0, 1)
    f["has_game_events"] = bool(kills.any() or banner.any() or money.any())
    f["n"] = n
    return f


def category_scores(f: dict, game_id: str) -> dict[str, np.ndarray]:
    w = game(game_id)["weights"]
    if f["has_game_events"]:
        skill = (0.40 * f["kills"] + 0.30 * f["banner"] + 0.10 * f["money"] + 0.08 * f["tag_gunshot"]
                 + 0.06 * f["trans"] + 0.06 * f["lex_skill"])
    else:
        # no kill feed / banner evidence: weaker, gunfire and callouts only
        skill = 0.30 * f["tag_gunshot"] + 0.20 * f["trans"] + 0.30 * f["lex_skill"] + 0.10 * f["tag_explosion"] + 0.10 * f["jump"]
        skill *= 0.75
    funny = (0.35 * f["tag_laugh"] + 0.15 * f["laugh_tok"] + 0.30 * f["lex_funny"] + 0.10 * f["words_ps"]
             + 0.10 * f["jump"])
    scare = (0.30 * f["tag_scream"] + 0.10 * f["tag_gasp"] + 0.20 * f["jump"] * f["quiet_before"]
             + 0.15 * f["face"] + 0.15 * f["lex_scare"] + 0.10 * f["cutf"])
    # fallback bucket: only wins when nothing more specific explains the moment
    other = 0.8 * (0.25 * f["jump"] + 0.25 * f["lex_other"] + 0.25 * f["tag_shout"] + 0.25 * f["tag_cheer"])
    out = {"skill": skill, "funny": funny, "scare": scare, "other": other}
    # music under a moment is not the moment
    damp = 1.0 - 0.5 * np.clip(f["tag_music"] - 0.5, 0, 0.5) * 2
    return {c: _mean(out[c] * damp, 3) * w.get(c, 1.0) for c in CATEGORIES}


# ---------- peaks and windows ----------

def pick_peaks(score: np.ndarray, limit: int, floor: float, nms: int = 25, pct: float = 95.0) -> list[int]:
    if len(score) == 0 or limit <= 0:
        return []
    thr = floor
    if len(score) > 600:
        thr = max(floor, float(np.percentile(score, pct)) * 0.8)
    s = score.copy()
    peaks = []
    while len(peaks) < limit:
        i = int(np.argmax(s))
        if s[i] < thr or s[i] <= 0:
            break
        peaks.append(i)
        s[max(0, i - nms): i + nms + 1] = -1
    return peaks


def _speech_bounds(t: float, speech: list) -> tuple[float, float] | None:
    for a, b in speech:
        if a <= t <= b:
            return a, b
    return None


def make_window(peak: float, cat: str, duration: float, speech: list, min_len: float, max_len: float,
                kill_times: list[float] | None = None) -> tuple[float, float]:
    start, end = peak - LEAD[cat], peak + AFTER[cat]
    if kill_times:
        start = min(start, kill_times[0] - 8.0)
        end = max(end, kill_times[-1] + 5.0)
    # don't cut a sentence in half
    b = _speech_bounds(start, speech)
    if b and start - b[0] <= 6.0:
        start = b[0] - 0.2
    b = _speech_bounds(end, speech)
    if b and b[1] - end <= 6.0:
        end = b[1] + 0.3
    if end - start < min_len:
        extra = min_len - (end - start)
        start -= extra * 0.6
        end += extra * 0.4
    if end - start > max_len:
        lead_share = min(0.75, (peak - start) / max(1e-6, end - start))
        start = peak - max_len * lead_share
        end = start + max_len
    start, end = max(0.0, start), min(duration, end)
    if end - start < min_len:
        if start <= 0:
            end = min(duration, start + min_len)
        else:
            start = max(0.0, end - min_len)
    return round(start, 2), round(end, 2)


def merge_candidates(cands: list[dict], max_len: float, overlap_frac: float = 0.4, gap: float = 3.0) -> list[dict]:
    """Union overlapping windows. The higher-scored category wins; evidence is pooled."""
    cands = sorted(cands, key=lambda c: c["start"])
    out: list[dict] = []
    for c in cands:
        if out:
            p = out[-1]
            ov = min(p["end"], c["end"]) - max(p["start"], c["start"])
            shorter = min(p["end"] - p["start"], c["end"] - c["start"])
            if ov >= overlap_frac * shorter or (0 <= c["start"] - p["end"] < gap):
                union = max(p["end"], c["end"]) - min(p["start"], c["start"])
                if union <= max_len:
                    best, other = (p, c) if p["score"] >= c["score"] else (c, p)
                    merged = dict(best)
                    merged["start"] = min(p["start"], c["start"])
                    merged["end"] = max(p["end"], c["end"])
                    merged["evidence"] = _dedupe_evidence(p["evidence"] + c["evidence"])
                    merged["score"] = max(p["score"], c["score"]) + 0.1 * min(p["score"], c["score"])
                    merged["also"] = sorted({*p.get("also", []), *c.get("also", []), other["category"]} - {best["category"]})
                    out[-1] = merged
                    continue
                if ov > 0.5 * shorter:
                    if c["score"] > p["score"]:
                        out[-1] = c
                    continue
        out.append(c)
    return out


def _dedupe_evidence(ev: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in sorted(ev, key=lambda e: -e.get("weight", 0)):
        key = e["label"]
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ---------- evidence ----------

def collect_evidence(f: dict, events: list[dict], start: float, end: float, peak: int) -> list[dict]:
    n = f["n"]
    i0, i1 = max(0, int(start)), min(n, int(math.ceil(end)))
    ev: list[dict] = []
    kills = [e for e in events if e.get("kind") == "kill" and start <= e["t"] <= end]
    if kills:
        ts = sorted(k["t"] for k in kills)
        src = "game feed" if kills[0].get("src") == "gsi" else "kill feed OCR"
        region = "killfeed"
        span = ts[-1] - ts[0]
        label = (f"{len(ts)} kills in {span:.0f} s ({src})" if len(ts) > 1 else f"1 kill with your name as killer ({src})")
        ev.append({"kind": "kills", "tier": "observed", "label": label, "t": ts[0], "weight": 0.4 * min(1, len(ts) / 3),
                   "count": len(ts), "span": span, "times": ts, "region": region})
    deaths = [e for e in events if e.get("kind") == "death" and start <= e["t"] <= end]
    if deaths:
        ev.append({"kind": "death", "tier": "observed", "label": f"you died at {fmt_ts(deaths[0]['t'])} (feed)",
                   "t": deaths[0]["t"], "weight": 0.02})
    for e in events:
        if start <= e["t"] <= end and e.get("kind") in SKILL_BANNERS:
            src = "game feed" if e.get("src") == "gsi" else "on-screen text, OCR"
            ev.append({"kind": "banner", "tier": "observed", "label": f"{e['kind'].replace('_', ' ')} ({src})",
                       "t": e["t"], "weight": 0.3 * SKILL_BANNERS[e["kind"]], "banner": e["kind"],
                       "region": None if e.get("src") == "gsi" else "banner"})
    money = [e for e in events if e.get("kind") == "money" and start <= e["t"] <= end]
    if money:
        total = sum(int(e.get("amount", 0)) for e in money)
        span = money[-1]["t"] - money[0]["t"]
        ev.append({"kind": "money", "tier": "observed", "label": f"money popups +{total} in {span:.0f} s (OCR)",
                   "t": money[0]["t"], "weight": 0.1 * float(f["money"][min(n - 1, int(money[0]["t"]))]),
                   "region": "reward"})
    for g, label, wgt in (("laugh", "laughter", 0.4), ("scream", "screaming", 0.3), ("gunshot", "gunfire", 0.08),
                          ("explosion", "explosion", 0.08), ("shout", "shouting", 0.05), ("cheer", "cheering", 0.1),
                          ("gasp", "gasp", 0.1)):
        arr = f["tag_" + g][i0:i1]
        if len(arr) and arr.max() >= 0.35:
            j = int(np.argmax(arr))
            ev.append({"kind": "tag_" + g, "tier": "model", "label": f"{label} (audio model, p={arr.max():.2f})",
                       "t": float(i0 + j), "weight": wgt * float(arr.max())})
    jz = f["jump"][i0:i1]
    if len(jz) and jz.max() >= 0.5:
        j = int(np.argmax(jz))
        quiet = f["quiet_before"][i0 + j] > 0
        ev.append({"kind": "loud", "tier": "observed",
                   "label": "voice suddenly louder than the last minute" + (" after a quiet stretch" if quiet else ""),
                   "t": float(i0 + j), "weight": 0.1 * float(jz.max())})
    tr = f["trans"][i0:i1]
    if len(tr) and tr.max() >= 0.6:
        ev.append({"kind": "transients", "tier": "observed", "label": "burst of sharp sounds (gunfire-like)",
                   "t": float(i0 + int(np.argmax(tr))), "weight": 0.05})
    fc = f["face"][i0:i1]
    if len(fc) and fc.max() >= 0.5:
        ev.append({"kind": "face", "tier": "observed", "label": "facecam movement spike",
                   "t": float(i0 + int(np.argmax(fc))), "weight": 0.1 * float(fc.max())})
    quoted = set()
    for s0 in range(i0, i1):
        for cat, hit, text, t in f["cues"].get(s0, []):
            if text in quoted:
                continue
            quoted.add(text)
            quote = text if len(text) <= 70 else text[:67] + "..."
            ev.append({"kind": "speech_" + cat, "tier": "model", "label": f"said \"{quote}\"", "t": t,
                       "weight": 0.12, "cue": hit})
    return _dedupe_evidence(ev)


def reason_text(evidence: list[dict], limit: int = 3) -> str:
    top = [e for e in evidence if e["kind"] != "death"][:limit]
    return "; ".join(e["label"] for e in top) if top else "general activity peak (weak evidence)"


def evidence_quality(evidence: list[dict]) -> float:
    tiers = {e["tier"] for e in evidence if e.get("weight", 0) >= 0.08}
    if "observed" in tiers and any(e["kind"] in ("kills", "banner") for e in evidence):
        return 1.15
    if "model" in tiers:
        return 1.0
    return 0.85


# ---------- main ----------

def find_candidates(duration: float, audio: dict, transcript: dict, visual: dict | None, events: list[dict], *,
                    game_id: str, max_candidates: int, min_len: float, max_len: float, prefs: dict | None = None,
                    orientation: str = "horizontal", seed_factor: float = 1.0) -> list[dict]:
    f = build_features(duration, audio, transcript, visual, events)
    scores = category_scores(f, game_id)
    prefs = prefs or {}
    cat_w = prefs.get("category_weights") or {}
    speech = audio.get("speech", [])
    limit = int(max(4, max_candidates * 2 * seed_factor))
    raw: list[dict] = []
    for cat in CATEGORIES:
        for p in pick_peaks(scores[cat], limit, floor=0.12):
            kill_ts = None
            if cat == "skill":
                kill_ts = sorted(e["t"] for e in events if e.get("kind") == "kill" and abs(e["t"] - p) <= 15)
            s, e = make_window(float(p), cat, duration, speech, min_len, max_len, kill_ts or None)
            ev = collect_evidence(f, events, s, e, p)
            base = float(scores[cat][p])
            raw.append({"start": s, "end": e, "peak": float(p), "category": cat, "score": base,
                        "evidence": ev, "also": []})
    merged = merge_candidates(raw, max_len)
    for c in merged:
        c["evidence"] = collect_evidence(f, events, c["start"], c["end"], int(c["peak"]))
        q = evidence_quality(c["evidence"])
        pref = float(cat_w.get(c["category"], 1.0))
        dur_pref = prefs.get("duration")
        dur_mult = 1.0
        if dur_pref and dur_pref.get("target"):
            d = c["end"] - c["start"]
            dur_mult = 1.0 - min(0.15, abs(d - dur_pref["target"]) / max(1.0, dur_pref["target"]) * 0.2)
        c["pref_mult"] = round(pref * dur_mult, 3)
        c["quality"] = q
        c["rank"] = c["score"] * q * c["pref_mult"]
        c["reason"] = reason_text(c["evidence"])

    # full-clip candidate for short inputs (e.g. existing 120 s verticals)
    if duration <= max(max_len * 1.5, 180 if orientation == "vertical" else 0):
        whole = {c: float(scores[c].max()) if len(scores[c]) else 0.0 for c in CATEGORIES}
        cat = max(whole, key=whole.get)
        ev = collect_evidence(f, events, 0.0, duration, int(np.argmax(scores[cat])))
        merged = [c for c in merged if (c["end"] - c["start"]) < duration * 0.8]
        merged.append({"start": 0.0, "end": round(duration, 2), "peak": float(np.argmax(scores[cat])), "category": cat,
                       "score": whole[cat], "evidence": ev, "also": [], "full_clip": True, "pref_mult": 1.0,
                       "quality": evidence_quality(ev), "rank": max(whole.values()) * 1.5 + 1.0,
                       "reason": "whole clip; " + reason_text(ev)})

    selected = _select(merged, max_candidates)
    top = max((c["rank"] for c in selected), default=1.0) or 1.0
    for c in selected:
        c["rank_score"] = round(100.0 * c["rank"] / top, 1)
        c["score"] = round(c["score"], 4)
    return sorted(selected, key=lambda c: -c["rank_score"])


def _select(cands: list[dict], k: int) -> list[dict]:
    """Keep every category represented, then fill by rank."""
    by_cat: dict[str, list[dict]] = {}
    for c in sorted(cands, key=lambda c: -c["rank"]):
        by_cat.setdefault(c["category"], []).append(c)
    chosen: list[dict] = [c for c in cands if c.get("full_clip")]
    quota = max(2, k // 8)
    for cat in CATEGORIES:
        for c in by_cat.get(cat, [])[:quota]:
            if c not in chosen and len(chosen) < k:
                chosen.append(c)
    for c in sorted(cands, key=lambda c: -c["rank"]):
        if len(chosen) >= k:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen


def quiet_windows(duration: float, audio: dict, visual: dict | None, per_hour: int, length: float = 30.0,
                  avoid: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """Low-speech stretches with the most on-screen or gunfire activity, spread across the recording,
    so a silent play still gets a visual look."""
    n = int(math.ceil(duration))
    if n < length * 2 or per_hour <= 0:
        return []
    speech = interval_fraction(audio.get("speech", []), n)
    trans = onsets_per_second(np.asarray(audio["flux"], np.float32), n)
    act = _mean(trans, int(length))
    if visual and visual.get("t"):
        mot = np.zeros(n, np.float32)
        idx = np.clip(np.asarray(visual["t"]).astype(int), 0, n - 1)
        np.maximum.at(mot, idx, np.asarray(visual.get("gp") or visual["motion"], np.float32))
        act = act + _mean(mot, int(length)) / (np.percentile(mot, 90) + 1e-3)
    sp = _mean(speech, int(length))
    count = max(1, int(round(duration / 3600 * per_hour)))
    strata = np.linspace(0, n, count + 1).astype(int)
    out = []
    avoid = avoid or []
    for a, b in pairwise(strata):
        if b - a < length:
            continue
        seg_score = act[a:b] * (sp[a:b] < 0.35)
        for _ in range(3):
            j = int(np.argmax(seg_score))
            c = a + j
            w = (max(0.0, c - length / 2), min(duration, c + length / 2))
            if seg_score[j] <= 0:
                break
            if any(min(w[1], y) - max(w[0], x) > 0 for x, y in avoid):
                seg_score[max(0, j - int(length)): j + int(length)] = 0
                continue
            out.append((round(w[0], 2), round(w[1], 2)))
            break
    return out

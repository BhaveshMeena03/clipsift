"""Hook text built only from what the clip contains: counted events or a sentence actually said."""
import re

from .lexicon import cue_hits

BANNER_HOOK = {"ace": "ACE", "team_ace": "Team ace", "clutch": "Clutch", "flawless": "Flawless round",
               "bomb_defused": "Bomb defused", "spike_defused": "Spike defused", "goal": "Goal",
               "aerial_goal": "Aerial goal", "epic_save": "Epic save", "hat_trick": "Hat trick",
               "demolition": "Demolition"}
TRUSTED = {"ok", "verified"}


def sentences(segments: list[dict], start: float, end: float) -> list[dict]:
    """Whole sentences said inside [start, end], split on . ? ! from trusted segments."""
    out = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end or seg.get("status") not in TRUSTED:
            continue
        cur: list[dict] = []
        for w in seg.get("words", []):
            if not start <= w["start"] <= end:
                continue
            cur.append(w)
            if re.search(r"[.?!]$", w["text"].strip()):
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
    res = []
    for ws in out:
        text = "".join(w["text"] for w in ws).strip()
        probs = [w["prob"] for w in ws if w.get("prob") is not None]
        res.append({"text": text, "abs_start": ws[0]["start"], "min_prob": min(probs) if probs else 0.0})
    return res


def hook_options(evidence: list[dict], sents: list[dict], max_quotes: int = 2) -> list[dict]:
    opts: list[dict] = []
    for e in evidence:
        if e["kind"] == "kills" and e.get("count", 0) >= 2:
            secs = round(e.get("span", 0))
            when = "at once" if e.get("span", 0) < 1 else "in 1 second" if secs <= 1 else f"in {secs} seconds"
            opts.append({"text": f"{e['count']} kills {when}", "source": "evidence",
                         "grounding": e["label"]})
        elif e["kind"] == "banner" and e.get("banner") in BANNER_HOOK:
            opts.append({"text": BANNER_HOOK[e["banner"]], "source": "evidence", "grounding": e["label"]})
    scored = []
    for s in sents:
        text = s["text"]
        n_words = len(text.split())
        if not 2 <= n_words <= 10 or len(text) > 56 or s["min_prob"] < 0.5:
            continue
        hits = cue_hits(text)
        score = sum(len(v) for v in hits.values()) + (0.5 if re.search(r"[!?]$", text) else 0)
        if score > 0:
            scored.append((score, text, s))
    scored.sort(key=lambda x: -x[0])
    quotes = 0
    for _score, text, s in scored:
        if quotes >= max_quotes:
            break
        opts.append({"text": f"\"{text.rstrip('.,')}\"", "source": "quote", "grounding": f"said at {s['abs_start']:.1f}s"})
        quotes += 1
    out, seen = [], set()
    for o in opts:
        if o["text"].lower() not in seen:
            seen.add(o["text"].lower())
            out.append(o)
    return out[:4]

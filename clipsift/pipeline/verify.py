"""Caption check. Unclear lines are decoded again with surrounding audio and preceding text as
context. Agreement verifies a line; disagreement flags it; two weak readings hide it until
reviewed. Nothing is ever filled in that the model did not hear."""
import logging
import re

from rapidfuzz.distance import Levenshtein

from ..analysis import asr
from ..media.audio import load_audio
from ..util import atomic_write_json, read_json

log = logging.getLogger(__name__)
LOW = 0.45


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def _mean_p(words: list[dict]) -> float:
    ps = [w["prob"] for w in words if w.get("prob") is not None]
    return sum(ps) / len(ps) if ps else 0.0


def needs_check(seg: dict) -> bool:
    if seg.get("status") in ("dropped", "verified", "revised", "disputed", "unclear"):
        return False
    if seg.get("status") == "flagged":
        return True
    return any(w.get("prob") is not None and w["prob"] < LOW for w in seg.get("words", []))


def alt_words_for(seg: dict, alt_segs: list[dict]) -> list[dict]:
    """Words from the second listen that belong to this line: centred inside it and not stretched
    over seconds of silence (a sign the model is guessing)."""
    a, b = seg["start"] - 0.3, seg["end"] + 0.3
    longest = max(1.5, 2 * (seg["end"] - seg["start"]))
    out = []
    for s in alt_segs:
        if "hallucination_phrase" in s.get("flags", []):
            continue
        for w in s.get("words", []):
            mid = (w["start"] + w["end"]) / 2
            if a <= mid <= b and w["end"] - w["start"] <= longest:
                out.append(w)
    return out


def decide(seg: dict, alt_words: list[dict]) -> dict:
    orig_p = _mean_p(seg.get("words", []))
    alt_text = "".join(w["text"] for w in alt_words).strip()
    if not alt_words:
        return {"status": "unclear" if orig_p < 0.5 else "disputed", "alt_text": "", "ratio": 0,
                "orig_p": round(orig_p, 3), "alt_p": 0.0}
    alt_p = _mean_p(alt_words)
    # word-level agreement: two different words in a short line is a real disagreement
    a, b = _norm(seg.get("text", "")).split(), _norm(alt_text).split()
    ratio = 100.0 * Levenshtein.normalized_similarity(a, b) if (a or b) else 100.0
    res = {"alt_text": alt_text, "ratio": round(ratio, 1), "orig_p": round(orig_p, 3), "alt_p": round(alt_p, 3)}
    if ratio >= 80:
        res["status"] = "verified"
    elif alt_p >= orig_p + 0.10:
        res["status"] = "revised"
        res["alt_words"] = alt_words
    elif max(orig_p, alt_p) < 0.5:
        res["status"] = "unclear"
    else:
        res["status"] = "disputed"
    return res


def verify_windows(ctx, path, transcript: dict, windows: list[tuple[float, float]], vdir, *, plan, voice_tracks,
                   lang, glossary, transcriber=None, on_progress=None) -> dict:
    segs = transcript["segments"]
    targets = [s for s in segs if needs_check(s)
               and any(s["start"] < b + 1 and s["end"] > a - 1 for a, b in windows)]
    if not targets:
        return transcript
    by_id = {s["id"]: i for i, s in enumerate(segs)}
    own = None
    try:
        for k, seg in enumerate(targets):
            ctx.check()
            f = vdir / f"{seg['id']}.json"
            res = read_json(f)
            if res is None:
                if transcriber is None and own is None:
                    own = asr.Transcriber(plan, ctx.paths.models, cpu_model=ctx.settings.get("asr_model_cpu", "small"))
                tr = transcriber or own
                a0 = max(0.0, seg["start"] - 4.0)
                pcm = load_audio(path, a0, seg["end"] - a0 + 4.0, voice_tracks, cancel=ctx.cancelled)
                i = by_id[seg["id"]]
                context = " ".join(s["text"] for s in segs[max(0, i - 2):i] if s.get("status") in ("ok", "verified"))
                prompt = (glossary.prompt() + ". " + context)[-400:].strip(". ")
                alt_segs = tr.transcribe(pcm, offset=a0, language_mode=lang, prompt=prompt, beam_size=8, vad=False,
                                         cancel=ctx.cancelled, seg_prefix=seg["id"] + "v")
                alt_words = alt_words_for(seg, alt_segs)
                res = decide(seg, alt_words)
                atomic_write_json(f, res)
            apply_result(seg, res, glossary)
            if on_progress:
                on_progress((k + 1) / len(targets))
    finally:
        if own is not None:
            own.close()
    segs.sort(key=lambda x: (x["start"], x["end"]))
    transcript["words"] = sorted((dict(w, seg=s["id"]) for s in segs for w in s.get("words", [])),
                                 key=lambda w: (w["start"], w["end"]))
    return transcript


def apply_result(seg: dict, res: dict, glossary) -> None:
    seg["status"] = res["status"]
    seg["check"] = {k: res.get(k) for k in ("ratio", "orig_p", "alt_p")}
    if res["status"] == "revised" and res.get("alt_words"):
        seg["prev_text"] = seg.get("text", "")
        seg["words"] = glossary.apply_words([dict(w) for w in res["alt_words"]])
        seg["text"] = "".join(w["text"] for w in seg["words"]).strip()
    elif res["status"] in ("disputed", "unclear") and res.get("alt_text"):
        seg["alt_text"] = res["alt_text"]

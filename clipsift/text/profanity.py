"""Light swear filter for captions and audio. Works on the words the speech model wrote,
so a word it misheard will not be caught. Not a complete filter, especially for Hindi."""
import re

_PATTERNS = [
    # English
    r"f+u+c+k+\w*", r"f+u+k+(?:ing|ed|er|in)?", r"f+c+k+\w*", r"motherf\w*", r"m+f+", r"wtf",
    r"s+h+i+t+(?:s|ty|ting|ted|head|hole)?", r"bull?s+h+i+t+", r"b+i+t+c+h+\w*", r"a+s+s+h+o+l+e+s?",
    r"d+i+c+k+(?:head|heads|s)?", r"p+u+s+s+y+", r"c+u+n+t+s?", r"wh+o+r+e+s?", r"s+l+u+t+s?",
    r"n+i+g+g+(?:a|as|er|ers|uh|az)", r"f+a+g+(?:got|gots|s)?", r"retard(?:ed|s)?",
    # Hindi / Hinglish, romanized
    r"m+a+a?d+[ae]+r+c+h+o+d+\w*", r"m+a+a?d+[ae]+r+j+a+a?t+", r"mc", r"bc", r"bkl",
    r"b+e+h?e?n+c+h+o+d+\w*", r"b+h?e+n+c+h+o+d?\w*", r"b+e+n+c+h+o+d+\w*",
    r"c+h+u+t+i+y?[aeu]+\w*", r"c+h+u+t+\w{0,3}", r"b+h+o+s+a?d+\w*", r"bsdk",
    r"g+a+a?n+d+u+", r"g+a+a+n+d+", r"l+a+u+d+[ae]+\w*", r"l+a+[vw]+d+[ae]+\w*", r"l+o+d+[aeu]+", r"l+u+n+d+",
    r"r+a+n+d+i+\w*", r"h+a+r+a+m+i+\w*", r"j+h+a+a?t+u+", r"t+a+t+t+i+",
]
_WORD_RE = re.compile(r"^(?:" + "|".join(_PATTERNS) + r")$", re.I)
_CORE = re.compile(r"^([^\w*]*)([\w*'\-]+?)([^\w*]*)$", re.U)


class Profanity:
    def __init__(self, extra: list[str] | None = None, allow: list[str] | None = None):
        self.extra = {w.strip().lower() for w in (extra or []) if w.strip()}
        self.allow = {w.strip().lower() for w in (allow or []) if w.strip()}

    def is_profane(self, word: str) -> bool:
        core = _norm(word)
        if not core or core in self.allow:
            return False
        if core in self.extra:
            return True
        return bool(_WORD_RE.match(core))

    def mask_word(self, word: str) -> str:
        m = _CORE.match(word)
        if not m:
            return word
        lead, core, trail = m.groups()
        if not self.is_profane(core):
            return word
        return lead + mask(core) + trail

    def mask_text(self, text: str) -> str:
        return re.sub(r"\S+", lambda m: self.mask_word(m.group(0)), text)


def _norm(word: str) -> str:
    w = re.sub(r"[^\w]", "", word.lower())
    return re.sub(r"(.)\1{2,}", r"\1\1", w)


def mask(core: str) -> str:
    """fuck -> f**k, bc -> b*, keep first and last letter."""
    if len(core) <= 1:
        return "*"
    if len(core) == 2:
        return core[0] + "*"
    return core[0] + "*" * (len(core) - 2) + core[-1]


def mute_spans(words: list[dict], prof: Profanity, pad: float = 0.06, join_gap: float = 0.15) -> list[tuple[float, float]]:
    """Time spans to silence for profane words. Words need 'start', 'end', 'text'."""
    spans = []
    for w in words:
        if w.get("start") is None or w.get("end") is None:
            continue
        if prof.is_profane(w.get("text", "")):
            spans.append((max(0.0, w["start"] - pad), w["end"] + pad))
    spans.sort()
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= join_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]

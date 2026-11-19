"""Turn OCR reads of the kill feed and banner regions into events.
OCR text is observed; 'your kill' depends on name position and is labeled as read from the feed."""
import re

from rapidfuzz import fuzz

from .games import game


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


# characters OCR swaps from one frame to the next ("sova123" read as "soval23", "VEPSN" as "VBPSN")
_OCR_LOOKALIKES = str.maketrans("01i58e2", "ollsbbz")
# thin glyphs OCR adds, drops or swaps around separators ("PRG | LEVELUP ツ" read as "PRGILEVELUPY")
_THIN = re.compile(r"[ly]")


def row_key(text: str) -> str:
    """Identity of a kill feed row that survives OCR flicker: lookalikes folded, separators and thin glyphs
    dropped. Names that differ by a real character stay different ("player1" vs "player2")."""
    return _THIN.sub("", _norm(text).translate(_OCR_LOOKALIKES))


def row_sig(texts: list[str]) -> str:
    return row_key(" ".join(texts))


# a bracketed prefix read together with a name: a clan tag ("[TH]HarukiXD") or a kill distance ("[41m] Forty")
_PREFIX = re.compile(r"^\W*(?:[\[(][^\])]{0,12}[\])]\W*)+")


def name_match(text: str, names: list[str], threshold: int = 85) -> bool:
    t = _norm(_PREFIX.sub("", text)) or _norm(text)
    if len(t) < 2:
        return False
    for n in names:
        nn = _norm(n)
        if not nn:
            continue
        if t == nn:
            return True
        if len(t) < 3 or len(nn) < 3:
            continue  # short words ("Me" in Valorant) only ever match exactly
        if len(t) >= 5 and fuzz.ratio(t, nn) >= threshold:
            return True
        if len(nn) >= 6 and len(t) >= len(nn) - 2 and fuzz.partial_ratio(nn, t) >= 92:
            return True
    return False


# a kill distance before the victim (Wardogs), also misread: "[41m]", "62m", "[34.m]", "17mm", "157ml" for "[57m]"
_DISTANCE = re.compile(r"^\W*(\d{1,3})[\s.]*m+[\])|lI1]?(?![a-z])")
# digits OCR swaps in the small feed font
_DIGIT_SWAPS = {frozenset("35"), frozenset("68"), frozenset("17")}


def _victim_side(text: str) -> str:
    text = text.strip()
    return text.split(" ", 1)[1] if " " in text else text


def victim_key(text: str) -> str:
    """The victim side of one of your kill rows ("[41m] Haruki" in "Forty [41m] Haruki"), folded like row_key but
    keeping names in other scripts ("爱笑的草丛")."""
    return _THIN.sub("", re.sub(r"[\W_]+", "", _victim_side(text).lower()).translate(_OCR_LOOKALIKES))


def victim_name_key(text: str) -> str:
    """Only the Latin part of the victim's name, without the distance."""
    return row_key(_DISTANCE.sub("", _victim_side(text)))


def victimless(text: str) -> bool:
    """A kill row read without its victim, only the killer and maybe a distance ("Forty [37m]", "Forty 62m")."""
    return len(re.sub(r"[\W_]+", "", _DISTANCE.sub("", _victim_side(text)))) <= 1


def kill_distance(text: str) -> str | None:
    m = _DISTANCE.match(_victim_side(text))
    return m.group(1) if m else None


def same_distance(a: str, b: str) -> bool:
    """Two reads of one distance: equal, a lost "[" read as a leading 1 ("18m]" for "[8m]"), or one digit swapped."""
    if len(a) != len(b):
        long, short = (a, b) if len(a) > len(b) else (b, a)
        return len(long) == len(short) + 1 and long[0] == "1" and long[1:] == short
    swaps = [frozenset(p) for p in zip(a, b, strict=True) if p[0] != p[1]]
    return not swaps or (len(swaps) == 1 and swaps[0] in _DIGIT_SWAPS)


def group_rows(items: list[dict], tol: float = 0.6) -> list[list[dict]]:
    """items: {text, x0, x1, y0, y1}. Rows by vertical overlap."""
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda i: (i["y0"] + i["y1"]) / 2):
        cy = (it["y0"] + it["y1"]) / 2
        h = max(1.0, it["y1"] - it["y0"])
        for row in rows:
            r = row[0]
            rcy = (r["y0"] + r["y1"]) / 2
            if abs(cy - rcy) < tol * max(h, r["y1"] - r["y0"]):
                row.append(it)
                break
        else:
            rows.append([it])
    return [sorted(r, key=lambda i: i["x0"]) for r in rows]


def killfeed_rows(ocr_items: list[dict], names: list[str]) -> list[dict]:
    out = []
    for row in group_rows(ocr_items):
        texts = [i for i in row if len(_norm(i["text"])) >= 2]
        if not texts:
            continue
        sig = row_sig([i["text"] for i in texts])
        left, right = texts[0], texts[-1]
        mine_left, mine_right = name_match(left["text"], names), name_match(right["text"], names)
        kind = "feed"
        if mine_right and len(texts) >= 2:
            kind = "death"  # also your name on both sides: a grenade or a fall, not a kill
        elif mine_left and len(texts) >= 2:
            kind = "kill"
        elif len(texts) == 1 and name_match(left["text"], names):
            kind = "kill?"  # icon-only row; we cannot tell which side
        out.append({"kind": kind, "sig": sig, "text": " ".join(i["text"] for i in texts)})
    return out


def names_you(ocr_items: list[dict], names: list[str]) -> bool:
    """One of the lines, or a word in one ("Forty threw3grenades thatround."), is your name."""
    return any(name_match(it["text"], names) or any(name_match(w, names) for w in it["text"].split())
               for it in ocr_items)


def banner_events(ocr_items: list[dict], game_id: str, names: list[str] = ()) -> list[dict]:
    g = game(game_id)
    words = g.get("banner_words") or {}
    needs_name = set(g.get("banner_needs_name") or ())
    text = " ".join(i["text"] for i in ocr_items).lower()
    text = re.sub(r"\s+", " ", text)
    hits = []
    for ev, phrases in words.items():
        if ev in needs_name and not names_you(ocr_items, list(names)):
            continue  # someone else's
        for ph in phrases:
            if len(ph) <= 4:
                ok = re.search(rf"\b{re.escape(ph)}\b", text) is not None
            else:
                # fuzzy only when the read is at least as long as the phrase ("ace" must not match "team ace")
                ok = ph in text or (len(text) >= len(ph) and fuzz.partial_ratio(ph, text) >= 90)
            if ok:
                hits.append({"kind": ev, "text": text[:80]})
                break
    # 'team ace' also matches 'ace'; keep the longer one
    kinds = {h["kind"] for h in hits}
    if "team_ace" in kinds:
        hits = [h for h in hits if h["kind"] != "ace"]
    if "epic_save" in kinds:
        hits = [h for h in hits if h["kind"] != "save"]
    if "aerial_goal" in kinds:
        hits = [h for h in hits if h["kind"] != "goal"]
    return hits


_MONEY = re.compile(r"(?:\+\s*)?[$₹]\s*(\d[\d,]{0,7})|\+\s*(\d[\d,]{1,7})\b|(\d[\d,]{1,7})\s*[$₹]")


def reward_amounts(ocr_items: list[dict]) -> list[int]:
    """Money popups like '+$300', '$ 1,200', '+150'. Returns the amounts read."""
    out = []
    for it in ocr_items:
        text = re.sub(r"^\+S(?=\s*\d)", "+$", it["text"])  # OCR often reads $ as S
        for m in _MONEY.finditer(text):
            raw = next(g for g in m.groups() if g)
            try:
                val = int(raw.replace(",", ""))
            except ValueError:
                continue
            if 0 < val <= 100000:
                out.append(val)
    return out


class FeedTracker:
    """Kill feed rows stay on screen for seconds; count each row once."""

    def __init__(self, ttl: float = 9.0):
        self.ttl = ttl
        self.seen: dict[str, float] = {}

    def update(self, t: float, rows: list[dict]) -> list[dict]:
        new = []
        for r in rows:
            last = self.seen.get(r["sig"])
            if last is None or t - last > self.ttl:
                new.append({**r, "t": t})
            self.seen[r["sig"]] = t
        self.seen = {k: v for k, v in self.seen.items() if t - v <= self.ttl}
        return new


class BannerTracker:
    def __init__(self, ttl: float = 6.0):
        self.ttl = ttl
        self.last: dict[str, float] = {}

    def update(self, t: float, events: list[dict]) -> list[dict]:
        new = []
        for e in events:
            last = self.last.get(e["kind"])
            if last is None or t - last > self.ttl:
                new.append({**e, "t": t})
            self.last[e["kind"]] = t
        return new

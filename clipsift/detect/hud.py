"""Turn OCR reads of the kill feed and banner regions into events.
OCR text is observed; 'your kill' depends on name position and is labeled as read from the feed."""
import re

from rapidfuzz import fuzz

from .games import game


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def name_match(text: str, names: list[str], threshold: int = 85) -> bool:
    t = _norm(text)
    if len(t) < 3:
        return False
    for n in names:
        nn = _norm(n)
        if not nn:
            continue
        if t == nn or (len(t) >= 5 and fuzz.ratio(t, nn) >= threshold):
            return True
        if len(nn) >= 6 and len(t) >= len(nn) - 2 and fuzz.partial_ratio(nn, t) >= 92:
            return True
    return False


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
        sig = "|".join(_norm(i["text"]) for i in texts)
        left, right = texts[0], texts[-1]
        kind = "feed"
        if name_match(left["text"], names) and len(texts) >= 2:
            kind = "kill"
        elif name_match(right["text"], names) and len(texts) >= 2:
            kind = "death"
        elif len(texts) == 1 and name_match(left["text"], names):
            kind = "kill?"  # icon-only row; we cannot tell which side
        out.append({"kind": kind, "sig": sig, "text": " ".join(i["text"] for i in texts)})
    return out


def banner_events(ocr_items: list[dict], game_id: str) -> list[dict]:
    words = game(game_id).get("banner_words") or {}
    text = " ".join(i["text"] for i in ocr_items).lower()
    text = re.sub(r"\s+", " ", text)
    hits = []
    for ev, phrases in words.items():
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

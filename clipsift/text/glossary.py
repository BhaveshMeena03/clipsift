"""User glossary: names and terms the speech model gets wrong. 'aliases' are what it
tends to write, 'term' is how it should look."""
import re


def _toks(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.lower().strip()) if t]


def _clean(tok: str) -> str:
    return re.sub(r"[^\w']", "", tok.lower())


class Glossary:
    def __init__(self, entries: list[dict]):
        self.rules: list[tuple[list[str], str]] = []
        for e in entries or []:
            term = str(e.get("term", "")).strip()
            if not term:
                continue
            for alias in [term, *(e.get("aliases") or [])]:
                toks = [_clean(t) for t in _toks(str(alias))]
                toks = [t for t in toks if t]
                if toks:
                    self.rules.append((toks, term))
        # longest alias first so "forty seven frags" beats "seven"
        self.rules.sort(key=lambda r: -len(r[0]))
        self.terms = [str(e.get("term", "")).strip() for e in entries or [] if str(e.get("term", "")).strip()]

    def apply_words(self, words: list[dict]) -> list[dict]:
        """Merge/replace ASR words. Keeps timing: a merged word spans first start to last end."""
        if not self.rules or not words:
            return words
        out = []
        i = 0
        cleaned = [_clean(w.get("text", "")) for w in words]
        while i < len(words):
            hit = None
            for toks, term in self.rules:
                n = len(toks)
                if cleaned[i:i + n] == toks:
                    hit = (n, term)
                    break
            if hit:
                n, term = hit
                first, last = words[i], words[i + n - 1]
                trail = re.search(r"[^\w']*$", last.get("text", "")).group(0)
                lead = re.match(r"^\s*", first.get("text", "")).group(0)
                probs = [w.get("prob") for w in words[i:i + n] if w.get("prob") is not None]
                merged = dict(first)
                merged.update(text=lead + term + trail, end=last.get("end"),
                              prob=min(probs) if probs else first.get("prob"), glossary=True)
                out.append(merged)
                i += n
            else:
                out.append(words[i])
                i += 1
        return out

    def apply_text(self, text: str) -> str:
        words = [{"text": t} for t in re.split(r"(?<=\S)(?=\s)", text)]
        return "".join(w["text"] for w in self.apply_words(words))

    def prompt(self, limit_chars: int = 400) -> str:
        """Short vocabulary hint for Whisper's initial prompt."""
        out, size = [], 0
        for t in self.terms:
            if size + len(t) + 2 > limit_chars:
                break
            out.append(t)
            size += len(t) + 2
        return ", ".join(out)

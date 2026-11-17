"""Devanagari to casual Roman Hindi ("kya kar raha hai"), not academic IAST."""
import re
import unicodedata

from .hindi_words import COMMON, LOANWORDS

CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
}
NUKTA_CONS = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f", "य": "y", "र": "r"}
PRECOMPOSED_NUKTA = {"क़": "क", "ख़": "ख", "ग़": "ग", "ज़": "ज", "ड़": "ड", "ढ़": "ढ", "फ़": "फ", "य़": "य", "ऱ": "र"}
VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
          "ऋ": "ri", "ऑ": "o", "ऍ": "e", "ॲ": "e"}
MATRAS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ृ": "ri",
          "ॉ": "o", "ॅ": "e", "ॆ": "e", "ॊ": "o"}
VIRAMA, NUKTA, ANUSVARA, CHANDRA, VISARGA = "्", "़", "ं", "ँ", "ः"
DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
DEVA = re.compile(r"[ऀ-ॿ]")
TOKEN = re.compile(r"[ऀ-ॿ‌‍]+|[^ऀ-ॿ‌‍]+")


def has_devanagari(text: str) -> bool:
    return bool(DEVA.search(text or ""))


def _syllables(word: str):
    """Split into [cons_roman|None, vowel_roman|None, nasal, explicit_vowel] units."""
    for pre, base in PRECOMPOSED_NUKTA.items():
        word = word.replace(pre, base + NUKTA)
    word = word.replace("‌", "").replace("‍", "")
    units = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]
        if ch in CONS:
            roman = CONS[ch]
            if i + 1 < n and word[i + 1] == NUKTA:
                roman = NUKTA_CONS.get(ch, roman)
                i += 1
            unit = {"c": roman, "v": None, "nasal": False, "virama": False, "base": ch}
            if i + 1 < n and word[i + 1] in MATRAS:
                unit["v"] = word[i + 1]
                i += 1
            elif i + 1 < n and word[i + 1] == VIRAMA:
                unit["virama"] = True
                i += 1
            units.append(unit)
        elif ch in VOWELS:
            units.append({"c": None, "v": ch, "nasal": False, "virama": False, "base": ch})
        elif ch in (ANUSVARA, CHANDRA):
            if units:
                units[-1]["nasal"] = True
        elif ch == VISARGA:
            units.append({"c": "h", "v": None, "nasal": False, "virama": True, "base": ch})
        elif ch in ("।", "॥"):
            units.append({"punct": "."})
        else:
            d = ch.translate(DIGITS)
            if d != ch:
                units.append({"punct": d})
        i += 1
    return units


def _has_vowel(u) -> bool:
    return "punct" not in u and (u["v"] is not None or (u["c"] is not None and not u["virama"] and u.get("schwa", True)))


def _romanize_word(word: str) -> str:
    units = _syllables(word)
    cons_units = [u for u in units if "punct" not in u]
    # inherent 'a' everywhere it is not blocked by a matra or virama
    for u in cons_units:
        u["schwa"] = u["c"] is not None and u["v"] is None and not u["virama"]
    # final schwa drops, unless the word is a single letter or ends in a conjunct (mitra)
    if cons_units:
        last = cons_units[-1]
        if last["schwa"] and len(cons_units) > 1:
            prev = cons_units[-2]
            if not prev["virama"] or last["base"] not in ("र", "य", "व"):
                last["schwa"] = False
    # medial schwa deletion, right to left: a -> 0 / V C _ C V
    for idx in range(len(cons_units) - 2, 0, -1):
        u = cons_units[idx]
        if not u["schwa"] or u["nasal"]:
            continue
        prev, nxt = cons_units[idx - 1], cons_units[idx + 1]
        # never build a three-consonant cluster (kampani, not kampni)
        if prev["virama"] or prev["nasal"] or nxt["c"] is None:
            continue
        if _has_vowel(prev) and _has_vowel(nxt):
            u["schwa"] = False
    out = []
    for j, u in enumerate(units):
        if "punct" in u:
            out.append(u["punct"])
            continue
        if u["c"] is not None:
            c = u["c"]
            last_unit = j == len(units) - 1
            if u["base"] == "व":
                c = "v" if u["v"] in ("ि", "ी", "े", "ै") else "w"
            elif u["base"] == "फ" and c == "ph" and last_unit and u["v"] is None:
                c = "f"  # sirf, saaf, taraf
            out.append(c)
        vowel = ""
        if u["v"] is not None:
            if u["c"] is None:
                vowel = VOWELS[u["v"]]
                # e after a vowel sounds like 'ye' (gaye, aaye)
                if u["v"] == "ए" and j > 0 and out and out[-1][-1:] in "aeiou":
                    vowel = "ye"
                elif u["v"] in ("ई", "ऊ") and j > 0:
                    vowel = "i" if u["v"] == "ई" else "u"  # ladai, aaun
            else:
                vowel = MATRAS[u["v"]]
                nxt = units[j + 1] if j + 1 < len(units) else None
                closes = (nxt is not None and "punct" not in nxt and nxt["c"] is not None and nxt["v"] is None
                          and not nxt.get("schwa", False) and j + 1 == len(units) - 1)
                # long vowel before a final consonant: baat, theek, phool
                if closes and u["v"] in ("ा", "ी", "ू"):
                    vowel = {"ा": "aa", "ी": "ee", "ू": "oo"}[u["v"]]
        elif u["c"] is not None and u.get("schwa"):
            vowel = "a"
        out.append(vowel)
        if u["nasal"]:
            nxt = units[j + 1] if j + 1 < len(units) else None
            out.append("m" if nxt and nxt.get("c") in ("p", "b", "bh", "m", "ph") else "n")
    return "".join(out)


def romanize_word(word: str) -> str:
    if not has_devanagari(word):
        return word
    key = word.strip()
    if key in COMMON:
        return COMMON[key]
    if key in LOANWORDS:
        return LOANWORDS[key]
    parts = TOKEN.findall(key)
    return "".join(_romanize_word(p) if has_devanagari(p) else p for p in parts)


def romanize(text: str) -> str:
    """Romanize Devanagari runs, leave Latin text, spaces and punctuation alone."""
    if not text or not has_devanagari(text):
        return text
    text = unicodedata.normalize("NFC", text)
    out = []
    for tok in re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        m = re.match(r"^([^\w\u0900-\u0963\u0966-\u097F]*)(.*?)([^\w\u0900-\u0963\u0966-\u097F]*)$", tok)
        lead, core, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", tok, "")
        core = romanize_word(core)
        trail = trail.replace("।", ".").replace("॥", ".")
        out.append(lead + core + trail)
    return "".join(out)

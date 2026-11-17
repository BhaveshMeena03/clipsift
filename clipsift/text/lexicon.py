"""Speech cues per category, English + Hinglish. A match only says what was said, not what happened."""
import re

CUES = {
    "skill": [
        r"clutch", r"\bace\b", r"one ?tap", r"head ?shot", r"what a shot", r"flick", r"no ?scope", r"wall ?bang",
        r"collateral", r"triple( kill)?", r"quad( kill)?", r"\b[345] ?k\b", r"one left", r"last one", r"ek bacha",
        r"ek hai", r"one v (one|two|three|four|five)", r"1 ?v ?[1-5]", r"\bninja\b", r"defus(e|ed|ing)",
        r"spike", r"saved? (it|the round)", r"easy", r"let'?s go+", r"lets go+", r"too good", r"insane",
        r"cracked", r"diff\b", r"goal", r"what a save", r"demo(lition)?",
    ],
    "funny": [
        r"\b(ha){2,}", r"\bhehe+", r"\blol\b", r"\blmao", r"bro what", r"what are you doing", r"why would you",
        r"kya kar raha", r"kya kar rahe", r"pagal", r"paagal", r"chutiya", r"noob", r"sorry+", r"my bad", r"oops",
        r"oh no+", r"bhai+\b", r"yaar", r"kya scene", r"dimaag", r"bewakoof", r"idiot", r"bruh", r"are you serious",
        r"what was that", r"tu kya", r"tune kya",
    ],
    "scare": [
        r"oh (my )?god", r"what was that", r"nahi+ nahi+", r"bhago", r"\brun\b", r"jesus", r"holy", r"scared",
        r"\bdar+\b", r"darr", r"bhoot", r"a+h{2,}", r"help", r"mummy", r"get away", r"behind (me|you)", r"peeche",
        r"jump ?scare", r"what the", r"no no no",
    ],
    "other": [
        r"oh my god", r"\bomg\b", r"wtf", r"\bno way", r"are you kidding", r"\barey+", r"\babe+\b", r"\boye+\b",
        r"\bwow", r"\bwhat+\b", r"crazy", r"unbelievable",
    ],
}
_COMPILED = {k: [re.compile(p, re.I) for p in v] for k, v in CUES.items()}
LAUGH_TOKENS = re.compile(r"\b(?:ha){2,}h?\b|\bhehe+\b|\blol\b|\blmao\b|\(laugh\w*\)|\[laugh\w*\]", re.I)


def cue_hits(text: str) -> dict[str, list[str]]:
    out = {}
    for cat, pats in _COMPILED.items():
        hits = []
        for p in pats:
            m = p.search(text)
            if m:
                hits.append(m.group(0))
        if hits:
            out[cat] = hits
    return out

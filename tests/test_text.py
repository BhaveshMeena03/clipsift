import unicodedata

import pytest

from clipsift.text.captions import build_lines, display_text, word_times_for_line
from clipsift.text.glossary import Glossary
from clipsift.text.profanity import Profanity, mask, mute_spans
from clipsift.text.translit import romanize

# ---------- romanization ----------

ROMAN = [
    ("भाई क्या कर रहा है", "bhai kya kar raha hai"),
    ("समझा", "samjha"), ("करना", "karna"), ("लड़का", "ladka"), ("बोलता", "bolta"), ("अपना", "apna"),
    ("मतलब", "matlab"), ("बदलना", "badalna"), ("कहना", "kahna"), ("बात", "baat"), ("ठीक", "theek"),
    ("पागल", "pagal"), ("दिमाग", "dimaag"), ("हेडशॉट मार दिया", "headshot maar diya"), ("बंद", "band"),
    ("कंपनी", "kampani"), ("मित्र", "mitra"), ("सिर्फ", "sirf"), ("दोस्त", "dost"), ("डरावना", "darawna"),
    ("लड़ाई", "ladai"), ("हवा", "hawa"), ("जवाब", "jawaab"), ("एक बचा है भाई", "ek bacha hai bhai"),
    ("गए", "gaye"), ("ओह माय गॉड यार।", "oh my god yaar."), ("क्या हुआ?", "kya hua?"), ("(हाहा)", "(haha)"),
]


@pytest.mark.parametrize("dev,roman", ROMAN)
def test_romanize(dev, roman):
    assert romanize(dev) == roman


def test_romanize_leaves_latin_and_mixed():
    assert romanize("pagal hai kya यार") == "pagal hai kya yaar"
    assert romanize("GG ez, 1v3 clutch!") == "GG ez, 1v3 clutch!"
    assert romanize("") == ""


def test_romanize_nukta_forms_match():
    pre = "ज़ोर"  # precomposed nukta
    dec = unicodedata.normalize("NFD", pre)
    assert romanize(pre) == romanize(dec) == "zor"


def test_romanize_digits_and_danda():
    assert romanize("३ किल्स।") == "3 kills."


def test_romanize_output_is_ascii_for_hindi():
    text = "आज रात भूत आया और सब भाग गए, बहुत डरावना था भाई"
    out = romanize(text)
    assert out.isascii(), out


# ---------- profanity ----------

@pytest.fixture
def prof():
    return Profanity()


@pytest.mark.parametrize("word", ["fuck", "Fucking", "FUUUUCK", "motherfucker", "shit", "bullshit", "bitch",
                                  "asshole", "bc", "mc", "chutiya", "chutiye", "bhenchod", "behenchod",
                                  "madarchod", "bhosdike", "bsdk", "gaandu", "lauda", "randi", "wtf"])
def test_profane(prof, word):
    assert prof.is_profane(word)


@pytest.mark.parametrize("word", ["class", "assassin", "pass", "shitake", "hello", "chhod", "chal", "bhai",
                                  "mast", "scunthorpe", "analysis", "cocktail", "hell", "damn", "saala", "glass"])
def test_not_profane(prof, word):
    assert not prof.is_profane(word)


def test_mask_keeps_ends_and_punctuation(prof):
    assert mask("fuck") == "f**k"
    assert mask("bc") == "b*"
    assert prof.mask_text("what the fuck, bro!") == "what the f**k, bro!"
    assert prof.mask_text('"Shit."') == '"S**t."'


def test_allow_and_extra_lists():
    p = Profanity(extra=["noob"], allow=["bc"])
    assert p.is_profane("noob")
    assert not p.is_profane("bc")


def test_mute_spans_pad_and_merge(prof):
    words = [{"text": "what", "start": 1.0, "end": 1.2}, {"text": "the", "start": 1.2, "end": 1.3},
             {"text": "fuck", "start": 1.3, "end": 1.6}, {"text": "shit", "start": 1.7, "end": 1.9},
             {"text": "ok", "start": 5.0, "end": 5.2}, {"text": "bc", "start": 8.0, "end": 8.2}]
    spans = mute_spans(words, prof)
    assert len(spans) == 2
    a, b = spans[0]
    assert a == pytest.approx(1.24) and b == pytest.approx(1.96)
    assert spans[1][0] == pytest.approx(7.94)


def test_mute_spans_skip_untimed(prof):
    assert mute_spans([{"text": "fuck", "start": None, "end": None}], prof) == []


# ---------- glossary ----------

def test_glossary_merges_multiword_alias_with_timing():
    g = Glossary([{"term": "fortysevenfrags", "aliases": ["forty seven frags"]}])
    words = [{"text": " nice", "start": 0, "end": 0.3, "prob": 0.9},
             {"text": " forty", "start": 0.4, "end": 0.6, "prob": 0.8},
             {"text": " seven", "start": 0.6, "end": 0.8, "prob": 0.7},
             {"text": " frags!", "start": 0.8, "end": 1.1, "prob": 0.95}]
    out = g.apply_words(words)
    assert [w["text"] for w in out] == [" nice", " fortysevenfrags!"]
    assert out[1]["start"] == 0.4 and out[1]["end"] == 1.1 and out[1]["prob"] == 0.7


def test_glossary_normalizes_term_case_and_prefers_longest():
    g = Glossary([{"term": "CS2", "aliases": ["cs two"]}, {"term": "Sova", "aliases": ["so va"]}])
    assert g.apply_text("playing cs2 and cs two with so va") == "playing CS2 and CS2 with Sova"


def test_glossary_prompt_limit():
    g = Glossary([{"term": f"name{i}", "aliases": []} for i in range(200)])
    assert len(g.prompt(100)) <= 100


# ---------- captions ----------

def _words(spec):
    out = []
    for text, s, e, *p in spec:
        out.append({"text": " " + text, "start": s, "end": e, "prob": p[0] if p else 0.9, "seg": "s0"})
    return out


def test_lines_are_in_clip_sorted_and_never_overlap():
    words = _words([("a", 9.5, 10.2), ("b", 10.3, 10.5), ("c", 10.6, 10.8), ("d", 10.9, 11.0), ("e", 11.1, 11.3),
                    ("f", 14.0, 14.2), ("g", 14.25, 14.3), ("h", 29.9, 30.6)])
    lines = build_lines(words, 10.0, 30.0, max_words=4)
    assert lines
    prev_end = -1
    for ln in lines:
        assert 0 <= ln["start"] < ln["end"] <= 20.0
        assert ln["start"] >= prev_end
        prev_end = ln["end"]
    # 'a' midpoint 9.85 is outside the clip, 'h' midpoint 30.25 too
    assert "a" not in lines[0]["text"].split()
    assert all("h" not in ln["text"].split() for ln in lines)


def test_line_starts_at_its_first_word():
    words = _words([("hello", 12.0, 12.4), ("there", 12.5, 12.9)])
    ln = build_lines(words, 10.0, 20.0)[0]
    assert ln["start"] == pytest.approx(2.0)
    assert ln["end"] >= 2.9 - 1e-6


def test_lines_split_on_pause_and_punctuation_and_max_words():
    words = _words([("one", 0.0, 0.2), ("two", 0.3, 0.4), ("three", 0.5, 0.6), ("four", 0.7, 0.8),
                    ("five", 0.9, 1.0), ("six.", 1.1, 1.2), ("seven", 1.3, 1.4), ("eight", 3.0, 3.2)])
    lines = build_lines(words, 0, 10, max_words=4)
    texts = [ln["text"] for ln in lines]
    assert texts == ["one two three four", "five six.", "seven", "eight"]


def test_low_probability_flags_and_unclear_hides():
    words = _words([("maybe", 1.0, 1.3, 0.2), ("this", 1.4, 1.6, 0.9)])
    lines = build_lines(words, 0, 5)
    assert lines[0]["status"] == "flagged" and not lines[0]["hidden"]
    lines = build_lines(words, 0, 5, seg_status={"s0": "unclear"})
    assert lines[0]["status"] == "unclear" and lines[0]["hidden"]


def test_overrides_edit_approve_and_hide():
    words = _words([("helo", 1.0, 1.3, 0.3), ("wrld", 1.4, 1.6, 0.3)])
    ov = [{"abs_start": 1.0, "abs_end": 1.6, "text": "hello world", "approved": True}]
    ln = build_lines(words, 0, 5, overrides=ov)[0]
    assert ln["text"] == "hello world" and ln["edited"] and ln["status"] == "ok"
    ln = build_lines(words, 0, 5, overrides=[{"abs_start": 1.0, "abs_end": 1.6, "hidden": True}])[0]
    assert ln["hidden"]


def test_override_survives_small_trim():
    words = _words([("helo", 5.0, 5.3), ("wrld", 5.4, 5.6)])
    ov = [{"abs_start": 5.0, "abs_end": 5.6, "text": "hello world"}]
    for start in (0.0, 2.0, 4.5):
        ln = build_lines(words, start, start + 10, overrides=ov)[0]
        assert ln["text"] == "hello world"
        assert ln["start"] == pytest.approx(5.0 - start)


def test_edited_line_word_times_are_estimated_and_cover_line():
    line = {"edited": True, "text": "ab cdef", "abs_start": 2.0, "abs_end": 3.0, "words": []}
    wt = word_times_for_line(line)
    assert all(w["estimated"] for w in wt)
    assert wt[0]["start"] == 2.0 and wt[-1]["end"] == pytest.approx(3.0)
    assert wt[0]["end"] == pytest.approx(wt[1]["start"])


def test_display_text_masks_and_uppercases():
    p = Profanity()
    assert display_text("oh shit bro", prof=p, censor=True, upper=True) == "OH S**T BRO"
    assert display_text("oh shit bro", prof=p, censor=False) == "oh shit bro"

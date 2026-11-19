import json
import os
import random
from itertools import pairwise

import pytest

from clipsift.pipeline.chunks import has_speech, plan_chunks, stitch
from clipsift.pipeline.context import stage_dir
from clipsift.pipeline.process import resegment, word_speech_fraction
from clipsift.util import atomic_write_json, file_fingerprint, fmt_ts, parse_ts, read_json, safe_filename, unique_path

# ---------- timestamps ----------


@pytest.mark.parametrize("s,txt", [(0, "00:00"), (59.9, "00:59"), (61, "01:01"), (3600, "1:00:00"),
                                   (32400 + 125, "9:02:05")])
def test_fmt_ts(s, txt):
    assert fmt_ts(s) == txt


@pytest.mark.parametrize("txt,s", [("1:02:03", 3723), ("62:03", 3723), ("123.5", 123.5), ("0:00:01.25", 1.25)])
def test_parse_ts(txt, s):
    assert parse_ts(txt) == pytest.approx(s)


@pytest.mark.parametrize("bad", ["", "abc", "1:2:3:4", "-5", "1::2"])
def test_parse_ts_rejects(bad):
    with pytest.raises(ValueError):
        parse_ts(bad)


def test_fmt_parse_roundtrip_long_stream():
    for s in (0, 1, 59, 3599, 3600, 9 * 3600 + 59 * 60 + 59):
        assert parse_ts(fmt_ts(s)) == s


# ---------- chunk planning ----------

def _speech_every(duration, period=7.0, talk=5.0):
    out, t = [], 0.5
    while t < duration:
        out.append([t, min(duration, t + talk)])
        t += period
    return out


@pytest.mark.parametrize("duration", [30.0, 600.0, 620.0, 1300.0, 9 * 3600 + 17.3])
def test_keep_ranges_tile_the_whole_recording(duration):
    chunks = plan_chunks(duration, _speech_every(duration))
    assert chunks[0]["keep_from"] == 0.0
    assert chunks[-1]["keep_to"] > duration
    for a, b in pairwise(chunks):
        assert a["keep_to"] == b["keep_from"]          # no gap, no overlap in what is kept
        assert a["end"] > b["start"]                    # decode windows do overlap
        assert a["start"] <= a["keep_from"] and a["keep_to"] <= a["end"] + 1e-6
    for c in chunks:
        assert 0 <= c["start"] < c["end"] <= duration
        assert c["end"] - c["start"] <= 600 + 30 + 20 + 1


def test_cuts_land_in_silence():
    duration = 3000.0
    speech = _speech_every(duration, period=13.0, talk=10.0)
    chunks = plan_chunks(duration, speech)
    for c in chunks[1:]:
        cut = c["keep_from"]
        assert not any(a < cut < b for a, b in speech), cut


def test_stitch_keeps_each_word_once():
    random.seed(4)
    duration = 2000.0
    true_words = []
    t = 0.0
    while t < duration - 1:
        t += random.uniform(0.15, 1.5)
        true_words.append(round(t, 3))
    chunks = plan_chunks(duration, [[w, w + 0.1] for w in true_words])
    results = []
    for ch in chunks:
        ws = [{"start": w, "end": w + 0.1, "text": f" w{i}", "prob": 0.9}
              for i, w in enumerate(true_words) if ch["start"] <= w < ch["end"]]
        # one segment per chunk that spans the overlap edges
        results.append([{"start": ws[0]["start"], "end": ws[-1]["end"], "text": "x", "words": ws, "flags": []}] if ws else [])
    segs = stitch(chunks, results)
    got = [w["start"] for s in segs for w in s["words"]]
    assert got == sorted(got)
    assert got == true_words


def test_stitch_drops_wordless_segments_outside_keep_range():
    chunks = plan_chunks(1300.0, None)
    seg_in = {"start": 10.0, "end": 12.0, "text": "a", "words": [], "flags": []}
    t = chunks[0]["keep_to"] + 2  # decoded by chunk 0 but owned by chunk 1
    assert t < chunks[0]["end"]
    seg_dup = {"start": t, "end": t + 1, "text": "b", "words": [], "flags": []}
    out = stitch(chunks[:2], [[seg_in, seg_dup], []])
    assert [s["text"] for s in out] == ["a"]


def test_has_speech():
    assert has_speech({"start": 10, "end": 20}, [[19, 25]])
    assert not has_speech({"start": 10, "end": 20}, [[20, 25], [0, 10]])


# ---------- resegment ----------

def test_resegment_splits_long_silence():
    words = [{"start": 40.0, "end": 40.5, "text": " a"}, {"start": 40.6, "end": 41.0, "text": " b."},
             {"start": 190.0, "end": 190.5, "text": " c"}]
    out = resegment([{"start": 40.0, "end": 190.5, "text": "a b. c", "words": words, "flags": []}])
    assert [(s["start"], s["end"], s["text"]) for s in out] == [(40.0, 41.0, "a b."), (190.0, 190.5, "c")]


def test_word_speech_fraction():
    speech = [[10, 12], [50, 51]]
    starts = [s[0] for s in speech]
    ws = [{"start": 10.2, "end": 10.5}, {"start": 30, "end": 30.3}]
    assert word_speech_fraction(ws, speech, starts) == 0.5


# ---------- files ----------

def test_atomic_write_never_leaves_partial(tmp_path):
    f = tmp_path / "x.json"
    atomic_write_json(f, {"a": 1})
    with pytest.raises(TypeError):
        atomic_write_json(f, {"a": object()})
    assert read_json(f) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_read_json_corrupt_is_missing(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": 1')
    assert read_json(f, "dflt") == "dflt"


def test_file_fingerprint_changes(tmp_path):
    f = tmp_path / "v.bin"
    f.write_bytes(os.urandom(300_000))
    a = file_fingerprint(f)
    data = bytearray(f.read_bytes())
    data[-10] ^= 0xFF
    f.write_bytes(bytes(data))
    assert file_fingerprint(f) != a


@pytest.mark.parametrize("name,ok", [("a/b:c*?.mp4", "a_b_c__.mp4"), ("CON", "_CON"), ("  .hidden. ", "hidden"),
                                     ("", "clip")])
def test_safe_filename(name, ok):
    assert safe_filename(name) == ok


def test_unique_path(tmp_path):
    p = tmp_path / "a.mp4"
    assert unique_path(p) == p
    p.write_text("x")
    assert unique_path(p).name == "a_2.mp4"


# ---------- stage checkpoints ----------

def test_stage_dir_keeps_and_wipes(tmp_path):
    d = stage_dir(tmp_path, "s", {"a": (1, 2)})
    (d / "chunk_0000.json").write_text("{}")
    d = stage_dir(tmp_path, "s", {"a": [1, 2]})            # same after JSON round trip
    assert (d / "chunk_0000.json").exists()
    d = stage_dir(tmp_path, "s", {"a": [1, 3]})
    assert not (d / "chunk_0000.json").exists()
    assert json.loads((d / "_params.json").read_text()) == {"a": [1, 3]}


# ---------- where data lives ----------

def test_windows_data_lives_next_to_the_app(tmp_path, monkeypatch):
    from clipsift.config import default_home
    monkeypatch.delenv("CLIPSIFT_HOME", raising=False)
    app = tmp_path / "D_drive" / "ClipSift"
    app.mkdir(parents=True)
    assert default_home(is_windows=True, app_root=app) == app / "data"
    # setup -DataDir remembers the choice; PowerShell 5.1 may write a BOM
    (app / "data_dir.txt").write_bytes("\ufeffE:\\ClipSiftData\r\n".encode("utf-8"))
    assert str(default_home(is_windows=True, app_root=app)) == "E:\\ClipSiftData"
    monkeypatch.setenv("CLIPSIFT_HOME", str(tmp_path / "env_home"))
    assert default_home(is_windows=True, app_root=app) == (tmp_path / "env_home").resolve()

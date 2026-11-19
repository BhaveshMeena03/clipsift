import threading
import time

import pytest

from clipsift.db import DB
from clipsift.render.render import (
    RenderSpec,
    audio_graph,
    even,
    inside_fraction,
    panel_sizes,
    parse_loudnorm,
    video_graph,
    visible_rect,
)


def _spec(**kw):
    base = dict(src="x.mp4", width=1920, height=1080, fps=60.0, orientation="horizontal", start=10.0, end=40.0,
                layout={"facecam": [0, 0, 0.25, 0.25], "gameplay": [0.25, 0.0, 0.5, 1.0], "gameplay_mode": "crop",
                        "facecam_h": 0.3, "brand_h": 0.085, "brand_on": True},
                export_tracks=[0], mute_tracks=[0])
    base.update(kw)
    return RenderSpec(**base)


def test_even():
    assert even(1) == 2 and even(3) == 4 and even(1080) == 1080 and even(575.9) == 576


def test_panels_add_up():
    for fc in (0.2, 0.3, 0.38):
        sp = _spec(layout={**_spec().layout, "facecam_h": fc})
        a, b, c = panel_sizes(sp, (1080, 1920))
        assert a + b + c == 1920 and a % 2 == 0 and c % 2 == 0


def test_panel_too_small_rejected():
    sp = _spec(layout={**_spec().layout, "facecam_h": 0.6, "brand_h": 0.2})
    with pytest.raises(RuntimeError):
        panel_sizes(sp, (1080, 1920))


def test_horizontal_crop_graph():
    g, info = video_graph(_spec(), (1080, 1920), has_base=True, has_text=True)
    assert "crop=960:1080:480:0" in g          # gameplay rect in source pixels
    assert "crop=480:270:0:0" in g              # facecam
    assert "[comp][txt]overlay" in g and g.endswith("[vout]")
    assert info["panels"][0] == 576


def test_fit_and_full_modes_blur_fill():
    g, _ = video_graph(_spec(layout={**_spec().layout, "gameplay_mode": "fit"}), (1080, 1920), True, False)
    assert "gblur" in g and "force_original_aspect_ratio=decrease" in g
    g, _ = video_graph(_spec(layout={**_spec().layout, "gameplay_mode": "full"}), (1080, 1920), True, False)
    assert "crop=1920:1080:0:0" in g


def test_vertical_source_keeps_composition():
    sp = _spec(width=1080, height=1920, orientation="vertical")
    g, info = video_graph(sp, (1080, 1920), has_base=False, has_text=True)
    assert info["panels"] is None
    assert "crop=" not in g and "gblur" not in g     # no reframing, no facecam/brand panels
    assert "scale=1080:1920" in g
    sp = _spec(width=720, height=1280, orientation="vertical")
    g, _ = video_graph(sp, (1080, 1920), False, False)
    assert "gblur" not in g                          # same 9:16 aspect, just scaled
    sp = _spec(width=1080, height=1350, orientation="vertical")
    g, _ = video_graph(sp, (1080, 1920), False, False)
    assert "gblur" in g                              # 4:5 gets blurred fill, not cropped


def test_insets_in_graph():
    lay = {**_spec().layout, "hud_insets": [{"src": [0.7, 0.05, 0.29, 0.25], "pos": "tr", "scale": 0.4},
                                            {"src": [0.3, 0.1, 0.4, 0.2], "pos": "tc"}]}
    g, _ = video_graph(_spec(layout=lay), (1080, 1920), True, False)
    assert "split=4" in g and "[ins0]" in g and "[ins1]" in g and "(W-w)/2" in g


def test_audio_graph_mutes_mixed_track():
    g = audio_graph(_spec(mute_spans=[(1.0, 1.5), (3.0, 3.2)]))
    assert "volume=volume=0:enable='between(t,1.000,1.500)+between(t,3.000,3.200)'" in g
    assert g.endswith("[aout]")


def test_audio_graph_mutes_only_voice_tracks():
    g = audio_graph(_spec(export_tracks=[1, 2, 3], mute_tracks=[1, 2], mute_spans=[(2.0, 2.4)]))
    chains = g.split(";")
    assert "volume=volume=0" in chains[0] and "volume=volume=0" in chains[1]
    assert "volume=volume=0" not in chains[2]       # game track keeps playing
    assert "amix=inputs=3" in g


def test_audio_graph_none_without_tracks():
    assert audio_graph(_spec(export_tracks=[])) is None


def test_loudnorm_second_pass_is_linear():
    loud = {"input_i": "-20.1", "input_tp": "-3.0", "input_lra": "5.0", "input_thresh": "-30.0", "target_offset": "0.2"}
    g = audio_graph(_spec(), loud)
    assert "measured_I=-20.1" in g and "linear=true" in g


_REAL = ('[Parsed_loudnorm_2 @ 000001] \n{\n\t"input_i" : "-20.10",\n\t"input_tp" : "-3.00",\n\t"input_lra" : "5.00",'
         '\n\t"input_thresh" : "-30.00",\n\t"target_offset" : "0.20"\n}\n')
# a recording's own tags are printed too; this one tries to add a file-writing filter to the export
_TAGS = ('  Metadata:\n    comment         : {"input_i":"-20","input_tp":"-1","input_lra":"5","input_thresh":"-30",'
         '"target_offset":"0,ametadata=mode=print:file=ClipSift.bat"}\n')


def test_loudness_ignores_lookalike_json_in_file_tags():
    vals = parse_loudnorm(_TAGS + "size=N/A time=00:00:10.00\n" + _REAL)
    assert vals == {"input_i": -20.1, "input_tp": -3.0, "input_lra": 5.0, "input_thresh": -30.0, "target_offset": 0.2}
    assert "ametadata" not in audio_graph(_spec(), vals)


@pytest.mark.parametrize("text", [_TAGS, _TAGS.replace("  Metadata", "[Parsed_loudnorm_0 @ 1]"), "",
                                  _REAL.replace('"-20.10"', '"-inf"'), _REAL.replace('"-20.10"', '"-80"')])
def test_loudness_rejects_anything_but_real_finite_numbers(text):
    assert parse_loudnorm(text) is None


def test_audio_graph_refuses_non_numeric_loudness():
    bad = {"input_i": "-20", "input_tp": "-1", "input_lra": "5", "input_thresh": "-30", "target_offset": "0,amovie=x"}
    with pytest.raises(ValueError):
        audio_graph(_spec(), bad)


def test_visible_rect_and_inside():
    lay = _spec().layout
    vis = visible_rect(lay, 1920, 1080)
    assert vis[0] >= 0.25 - 1e-6 and vis[2] <= 0.75 + 1e-6
    killfeed = (0.70, 0.05, 0.29, 0.25)
    assert inside_fraction(killfeed, vis) < 0.3
    assert inside_fraction((0.4, 0.4, 0.1, 0.1), vis) == pytest.approx(1.0)
    assert visible_rect({**lay, "gameplay_mode": "full"}, 1920, 1080) == (0, 0, 1, 1)
    left = visible_rect({**lay, "pan": -1.0}, 1920, 1080)
    right = visible_rect({**lay, "pan": 1.0}, 1920, 1080)
    assert left[0] <= vis[0] <= right[0]


# ---------- DB ----------

@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_settings_roundtrip_and_unknown(db):
    db.set_settings({"max_candidates": 55, "ingame_names": ["a", "b"]})
    st = db.settings()
    assert st["max_candidates"] == 55 and st["ingame_names"] == ["a", "b"]
    with pytest.raises(KeyError):
        db.set_settings({"nope": 1})
    assert db.settings()["max_candidates"] == 55


def test_default_glossary_and_replace(db):
    assert any(g["term"] == "fortysevenfrags" for g in db.glossary())
    db.replace_glossary([{"term": "Raze", "aliases": ["raise"]}, {"term": " ", "aliases": []}])
    g = db.glossary()
    assert [x["term"] for x in g] == ["Raze"] and g[0]["aliases"] == ["raise"]


def test_claim_job_is_exclusive(tmp_path):
    path = tmp_path / "c.db"
    db = DB(path)
    ids = [db.add_job("export", "light") for _ in range(30)]
    got: list[int] = []
    lock = threading.Lock()

    def worker(n):
        d = DB(path)
        while True:
            j = d.claim_job("light", n)
            if not j:
                return
            with lock:
                got.append(j["id"])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got) == ids


def test_lanes_are_separate(db):
    db.add_job("process", "heavy")
    assert db.claim_job("light", 1) is None
    assert db.claim_job("heavy", 1)["kind"] == "process"


def test_requeue_and_crash_limit(db):
    jid = db.add_job("process", "heavy")
    for expected in (1, 2):
        db.claim_job("heavy", 1)
        assert db.requeue_interrupted() == [jid]
        assert db.job(jid)["crash_count"] == expected and db.job(jid)["status"] == "queued"
    db.claim_job("heavy", 1)
    assert db.requeue_interrupted() == []
    j = db.job(jid)
    assert j["status"] == "failed" and "3 times" in j["error"]


def test_json_columns_roundtrip(db):
    cid = db.insert("candidates", source_id=1, start=0.0, end=10.0, category="funny",
                    evidence_json=[{"kind": "x"}], edit_json={"hook": "hi"}, created_at=time.time())
    c = db.one("SELECT * FROM candidates WHERE id=?", (cid,))
    assert c["evidence"] == [{"kind": "x"}] and c["edit"] == {"hook": "hi"} and c["hooks"] == []


def test_watchdog_finds_stalled_job(tmp_path):
    from types import SimpleNamespace

    from clipsift.app import STALL_S, WorkerSupervisor
    from clipsift.config import Paths
    paths = Paths(tmp_path / "home").ensure()
    db = DB(paths.db)
    sup = WorkerSupervisor(paths, {})
    sup.proc = SimpleNamespace(pid=4242)
    jid = db.add_job("process", "heavy")
    db.claim_job("heavy", 4242)
    db.update("jobs", jid, updated_at=time.time() - 60)
    assert sup.stalled_job() is None
    db.update("jobs", jid, updated_at=time.time() - STALL_S - 5)
    assert sup.stalled_job()["id"] == jid
    db.update("jobs", jid, worker_pid=999)        # someone else's job
    assert sup.stalled_job() is None


def test_child_pid_is_the_real_python_and_kill_tree_ends_it():
    # found on Windows: .venv\Scripts\python.exe is a launcher, so Popen.pid was not the worker's own pid
    import subprocess
    import sys

    import psutil

    from clipsift.util import interpreter_pid, kill_tree
    p = subprocess.Popen([sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"],
                         stdout=subprocess.PIPE, text=True)
    try:
        own_pid = int(p.stdout.readline())
        assert interpreter_pid(p) == own_pid
    finally:
        kill_tree(p)
    assert not psutil.pid_exists(own_pid)

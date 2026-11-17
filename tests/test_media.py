"""End-to-end tests on real media files: ffmpeg, PyAV, speech model, OCR, tagger."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from clipsift.media.audio import load_audio
from clipsift.media.frames import iter_frames
from clipsift.media.probe import ProbeError, probe
from tests import synth
from tests.conftest import SYNTH_DIR, add_source, ffmpeg, make_home, needs_media, needs_models, run_job

pytestmark = [needs_media]

LAYOUT = {"facecam": [40 / 1920, 40 / 1080, 480 / 1920, 270 / 1080], "gameplay": [0.25, 0.0, 0.5, 1.0],
          "gameplay_mode": "crop", "facecam_h": 0.28, "brand_h": 0.08, "brand_on": True,
          "brand": {"text": "fortysevenfrags"}}


def sha256(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


@pytest.fixture(scope="session")
def stream():
    return synth.build(SYNTH_DIR)


@pytest.fixture(scope="session")
def processed(stream, tmp_path_factory):
    root = tmp_path_factory.mktemp("proc")
    paths, db = make_home(root)
    before = sha256(stream["path"])
    sid = add_source(db, stream["path"], game="valorant", layout=LAYOUT)
    t = time.time()
    job = run_job(paths, db, "process", "heavy", source_id=sid)
    return {"paths": paths, "db": db, "sid": sid, "job": job, "first_run_s": time.time() - t, "sha_before": before,
            "stream": stream}


# ---------- media primitives ----------

def test_audio_timestamps_are_exact(stream):
    if not synth.have_tts():
        pytest.skip("no TTS speech in this stream")
    a = load_audio(stream["path"], 35.0, 10.0)
    assert len(a) == 160000
    env = np.sqrt(np.convolve(a * a, np.ones(160) / 160, mode="same"))
    onset = np.argmax(env > 0.02) / 16000
    assert 35.0 + onset == pytest.approx(40.0, abs=0.25)


def test_frame_sampling_times(stream):
    ts = [t for t, _f in iter_frames(stream["path"], fps=2.0, start=100.0, end=104.9)]
    assert ts[0] == pytest.approx(100.0, abs=0.05)
    assert np.allclose(np.diff(ts), 0.5, atol=0.04)
    kts = [t for t, _f in iter_frames(stream["path"], keyframes_only=True, start=0.0, end=60.0)]
    assert kts == sorted(kts) and len(kts) >= 5
    assert all(abs(t / 10 - round(t / 10)) < 0.01 for t in kts)  # -g 300 at 30 fps


def test_probe_rejects_bad_files(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 5000)
    with pytest.raises(ProbeError):
        probe(bad)
    with pytest.raises(ProbeError):
        probe(tmp_path / "missing.mp4")


# ---------- detection on the labelled sample ----------

@needs_models
def test_pipeline_finds_labelled_moments(processed):
    assert processed["job"]["status"] == "done", processed["job"]["error"]
    db, sid = processed["db"], processed["sid"]
    labels = processed["stream"]["labels"]
    cands = db.all("SELECT * FROM candidates WHERE source_id=? ORDER BY rank_score DESC", (sid,))
    tol = labels["tolerance_s"]
    for m in labels["moments"]:
        if m.get("requires_tts") and not synth.have_tts():
            continue
        hit = [c for c in cands if c["start"] - tol <= m["t"] <= c["end"] + tol]
        assert hit, f"missed {m}"
        assert hit[0]["category"] == m["category"], (m, hit[0]["category"], hit[0]["reason"])
    skill = next(c for c in cands if c["category"] == "skill")
    assert skill["rank_score"] == 100.0
    assert any(e["kind"] == "kills" and e["count"] == 3 and e["tier"] == "observed" for e in skill["evidence"])
    for nh in labels["not_highlights"]:
        if nh.get("requires_tts") and not synth.have_tts():
            continue
        top2 = cands[:2]
        assert not any(c["start"] <= nh["t"] <= c["end"] for c in top2), "decoy ranked in top 2"


@needs_models
def test_resume_skips_finished_work(processed):
    paths, db, sid = processed["paths"], processed["db"], processed["sid"]
    work = paths.source_work(sid)
    chunk = work / "transcript" / "chunk_0000.json"
    vis = work / "visual" / "block_0000.json"
    m_chunk, m_vis = chunk.stat().st_mtime_ns, vis.stat().st_mtime_ns
    t = time.time()
    job = run_job(paths, db, "process", "heavy", source_id=sid)
    second = time.time() - t
    assert job["status"] == "done"
    assert chunk.stat().st_mtime_ns == m_chunk and vis.stat().st_mtime_ns == m_vis
    assert second < processed["first_run_s"]
    vis.unlink()
    job = run_job(paths, db, "process", "heavy", source_id=sid)
    assert job["status"] == "done" and vis.exists() and chunk.stat().st_mtime_ns == m_chunk


@needs_models
def test_cancel_then_resume_gives_same_candidates(stream, tmp_path, processed):
    paths, db = make_home(tmp_path)
    sid = add_source(db, stream["path"], game="valorant", layout=LAYOUT)
    jid = db.add_job("process", "heavy", source_id=sid)
    from clipsift.worker import Worker
    w = Worker(paths)
    job = db.claim_job("heavy", os.getpid())

    def cancel_when_scanning():
        for _ in range(600):
            j = db.job(jid)
            if j["stage"] in ("transcribe", "visual_scan") or j["status"] != "running":
                break
            time.sleep(0.05)
        db.update("jobs", jid, cancel_requested=1)

    th = threading.Thread(target=cancel_when_scanning)
    th.start()
    w.run_job(job)
    th.join()
    assert db.job(jid)["status"] == "cancelled"
    assert "Resume" in db.job(jid)["message"]
    db.update("jobs", jid, status="queued", cancel_requested=0)
    job = db.claim_job("heavy", os.getpid())
    w.run_job(job)
    assert db.job(jid)["status"] == "done"
    got = [(c["category"], round(c["start"]), round(c["end"])) for c in
           db.all("SELECT * FROM candidates WHERE source_id=? ORDER BY start", (sid,))]
    ref = [(c["category"], round(c["start"]), round(c["end"])) for c in
           processed["db"].all("SELECT * FROM candidates WHERE source_id=? ORDER BY start", (processed["sid"],))]
    assert got == ref


# ---------- export ----------

def _frame(path, t, y0=None, y1=None):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], capture_output=True, check=True)
    info = probe(path)
    img = np.frombuffer(r.stdout, np.uint8).reshape(info.height, info.width).astype(np.float32)
    return img[y0:y1] if y0 is not None else img


@needs_models
def test_export_and_caption_timing(processed):
    paths, db = processed["paths"], processed["db"]
    c = db.one("SELECT * FROM candidates WHERE source_id=? AND category='funny'", (processed["sid"],))
    from clipsift.pipeline.export import candidate_lines
    lines = [ln for ln in candidate_lines(paths, c, db.settings())["lines"] if not ln["hidden"]]
    assert lines
    t_cap = (lines[0]["start"] + lines[0]["end"]) / 2
    t_none = max(0.2, lines[0]["start"] - 1.5)
    job = run_job(paths, db, "export", "light", candidate_id=c["id"])
    assert job["status"] == "done", job["error"]
    out = job["result"]["path"]
    info = probe(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.audio and abs(info.duration - (c["end"] - c["start"])) < 0.3
    # same clip with captions off, to isolate the caption pixels
    edit = dict(c["edit"], captions_on=False, hook="")
    db.update("candidates", c["id"], edit_json=edit, status="new")
    job2 = run_job(paths, db, "export", "light", candidate_id=c["id"])
    out2 = job2["result"]["path"]
    assert out2 != out and os.path.exists(out)
    band = (int(1920 * 0.62), int(1920 * 0.78))
    with_cap = np.abs(_frame(out, t_cap, *band) - _frame(out2, t_cap, *band)).mean()
    no_cap = np.abs(_frame(out, t_none, *band) - _frame(out2, t_none, *band)).mean()
    assert with_cap > 4 * max(no_cap, 0.5), (with_cap, no_cap)


@needs_models
def test_source_is_never_modified(processed):
    assert sha256(processed["stream"]["path"]) == processed["sha_before"]
    exports = list(processed["paths"].exports.glob("*.mp4"))
    assert all(os.path.realpath(e) != os.path.realpath(processed["stream"]["path"]) for e in exports)


@needs_models
def test_preview_is_small_and_cached(processed):
    paths, db = processed["paths"], processed["db"]
    c = db.one("SELECT * FROM candidates WHERE source_id=? AND category='skill'", (processed["sid"],))
    j1 = run_job(paths, db, "preview", "light", candidate_id=c["id"])
    assert j1["status"] == "done", j1["error"]
    p = j1["result"]["path"]
    assert (probe(p).width, probe(p).height) == (540, 960)
    m = os.stat(p).st_mtime_ns
    j2 = run_job(paths, db, "preview", "light", candidate_id=c["id"])
    assert j2["result"]["path"] == p and os.stat(p).st_mtime_ns == m


@needs_models
def test_disk_full_fails_cleanly(processed, monkeypatch):
    from clipsift.media import disk
    monkeypatch.setattr(disk, "free_bytes", lambda _p: 10 * 1024**2)
    paths, db = processed["paths"], processed["db"]
    c = db.one("SELECT * FROM candidates WHERE source_id=? AND category='skill'", (processed["sid"],))
    before = set(paths.exports.glob("*"))
    job = run_job(paths, db, "export", "light", candidate_id=c["id"])
    assert job["status"] == "failed" and "disk space" in job["error"]
    assert set(paths.exports.glob("*")) == before


# ---------- separate media cases ----------

@pytest.fixture(scope="module")
def swear_clip(tmp_path_factory):
    if not synth.have_tts():
        pytest.skip("needs macOS TTS")
    d = tmp_path_factory.mktemp("swear")
    wav = synth.tts("okay. what the fuck is this, bro", "Rishi", d / "s.wav")
    out = d / "swear.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi", "-i", "sine=f=220:d=12:r=48000",
           "-i", str(wav), "-filter_complex", "[1:a]volume=0.02[b];[2:a]adelay=3000|3000,apad=whole_dur=12[s];"
           "[b][s]amix=inputs=2:normalize=0:duration=first[a]", "-map", "0:v", "-map", "[a]", "-t", "12",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out))
    return out


def _rms_db(path, a, b):
    x = load_audio(path, a, b - a)
    return 20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9)


@needs_models
def test_profanity_is_masked_and_muted(swear_clip, tmp_path):
    paths, db = make_home(tmp_path, loudnorm=False)
    sid = add_source(db, swear_clip)
    assert run_job(paths, db, "process", "heavy", source_id=sid)["status"] == "done"
    c = db.one("SELECT * FROM candidates WHERE source_id=? ORDER BY end-start DESC", (sid,))
    from clipsift.pipeline.export import candidate_lines
    cl = candidate_lines(paths, c, db.settings())
    shown = " ".join(ln["display"] for ln in cl["lines"])
    assert "f**k" in shown.lower() and "fuck" not in shown.lower(), shown
    assert len(cl["mute_spans"]) == 1
    a, b = cl["mute_spans"][0]
    on = run_job(paths, db, "export", "light", candidate_id=c["id"])["result"]["path"]
    db.update("candidates", c["id"], edit_json=dict(c["edit"], censor_on=False), status="new")
    off = run_job(paths, db, "export", "light", candidate_id=c["id"])["result"]["path"]
    mid_a, mid_b = a + 0.08, b - 0.08
    assert _rms_db(on, mid_a, mid_b) < -55
    assert _rms_db(off, mid_a, mid_b) > -40
    # the rest of the reaction is still there
    assert abs(_rms_db(on, 0.5, max(0.6, a - 0.3)) - _rms_db(off, 0.5, max(0.6, a - 0.3))) < 1.0


@needs_models
def test_vertical_source_is_not_reframed(tmp_path):
    src = tmp_path / "vert.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=20", "-f", "lavfi", "-i", "sine=f=330:d=20",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", "-shortest", str(src))
    paths, db = make_home(tmp_path)
    sid = add_source(db, src, layout=LAYOUT)
    assert run_job(paths, db, "process", "heavy", source_id=sid)["status"] == "done"
    c = db.one("SELECT * FROM candidates WHERE source_id=? AND end-start > 19", (sid,))
    assert c, "full-clip candidate expected for a short vertical"
    job = run_job(paths, db, "export", "light", candidate_id=c["id"])
    out = job["result"]["path"]
    a = _frame(out, 5.0, 100, 900)
    b = _frame(src, 5.0, 100, 900)
    assert np.abs(a - b).mean() < 6.0   # same picture: no facecam panel, no crop
    bottom = _frame(out, 5.0, 1800, 1920)
    assert bottom.std() > 5             # not a flat branding panel


def test_corrupt_and_silent_sources(tmp_path):
    from clipsift import models
    from tests.conftest import MODELS
    if not models.whisper_ready(MODELS, "hinglish-swift"):
        pytest.skip("models")
    paths, db = make_home(tmp_path)
    good = tmp_path / "noaudio.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=40", "-c:v", "libx264", "-preset", "ultrafast", str(good))
    trunc = tmp_path / "trunc.mp4"
    trunc.write_bytes(good.read_bytes()[:20000])
    sid = add_source(db, trunc)
    job = run_job(paths, db, "process", "heavy", source_id=sid)
    assert job["status"] == "failed" and job["error"]
    sid = add_source(db, good)
    job = run_job(paths, db, "process", "heavy", source_id=sid)
    assert job["status"] == "done", job["error"]
    assert any("no audio" in n for n in job["result"]["notes"])


def test_labels_file_is_valid_json(stream):
    data = json.loads((SYNTH_DIR / "labels.json").read_text())
    assert {"moments", "not_highlights", "tolerance_s"} <= set(data)
    shutil.which("ffmpeg")


@needs_models
@pytest.mark.parametrize("stage", ["visual_scan", "visual_detail"])
def test_cancel_during_video_decode_does_not_hang(stream, tmp_path, stage):
    """Cancelling mid-decode once deadlocked PyAV teardown. The job must stop promptly."""
    paths, db = make_home(tmp_path, visual_scan="1fps")
    sid = add_source(db, stream["path"], game="valorant", layout=LAYOUT)
    jid = db.add_job("process", "heavy", source_id=sid)
    from clipsift.worker import Worker
    w = Worker(paths)
    job = db.claim_job("heavy", os.getpid())
    seen = {}

    def cancel_in_stage():
        for _ in range(2400):
            j = db.job(jid)
            if j["stage"] == stage:
                time.sleep(0.8)  # let decoding get going
                seen["t"] = time.time()
                db.update("jobs", jid, cancel_requested=1)
                return
            if j["status"] != "running":
                return
            time.sleep(0.05)

    th = threading.Thread(target=cancel_in_stage)
    th.start()
    runner = threading.Thread(target=w.run_job, args=(job,), daemon=True)
    runner.start()
    runner.join(timeout=240)
    th.join(timeout=5)
    assert not runner.is_alive(), "worker hung after cancel"
    assert "t" in seen, f"never reached {stage}"
    assert db.job(jid)["status"] == "cancelled"
    assert time.time() - seen["t"] < 30

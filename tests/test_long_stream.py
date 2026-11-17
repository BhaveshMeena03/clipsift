"""Long recording: chunk boundaries, block checkpoints, and a hard kill of the worker mid-run."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import synth
from tests.conftest import add_source, ffmpeg, make_home, needs_media, needs_models

pytestmark = [needs_media, needs_models, pytest.mark.slow]
DUR = 1320.0  # 22 min: 3 transcription chunks, 3 audio blocks
SENTENCES = [(596.0, "this sentence crosses the first chunk boundary on purpose"),
             (1197.5, "and this one sits right on the second boundary as well")]


@pytest.fixture(scope="module")
def long_stream(tmp_path_factory):
    if not synth.have_tts():
        pytest.skip("needs macOS TTS")
    d = tmp_path_factory.mktemp("long")
    wavs = [synth.tts(text, "Rishi", d / f"s{i}.wav") for i, (_t, text) in enumerate(SENTENCES)]
    out = d / "long.mp4"
    parts = ["[1:a]volume=1[bg]"]
    mix = ["[bg]"]
    for i, (t, _text) in enumerate(SENTENCES):
        ms = int(t * 1000)
        parts.append(f"[{2 + i}:a]adelay={ms}|{ms},apad=whole_dur={DUR}[s{i}]")
        mix.append(f"[s{i}]")
    parts.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first[a]")
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=5:d={DUR}", "-f", "lavfi", "-i", f"anoisesrc=d={DUR}:c=pink:a=0.003",
           *sum([["-i", str(w)] for w in wavs], []), "-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]",
           "-t", str(DUR), "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", "-c:a", "aac", str(out))
    return out


def _worker(home: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    return subprocess.Popen([sys.executable, "-m", "clipsift.worker", "--home", str(home)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_hard_kill_mid_transcription_then_resume(long_stream, tmp_path):
    paths, db = make_home(tmp_path, visual_scan="keyframes", quiet_samples_per_hour=2)
    sid = add_source(db, long_stream)
    jid = db.add_job("process", "heavy", source_id=sid)
    tdir = paths.source_work(sid) / "transcript"
    w = _worker(paths.home)
    try:
        deadline = time.time() + 300
        while time.time() < deadline and not (tdir / "chunk_0000.json").exists():
            time.sleep(0.2)
        assert (tdir / "chunk_0000.json").exists(), "first chunk never finished"
        os.kill(w.pid, signal.SIGKILL)
        w.wait(timeout=10)
    finally:
        if w.poll() is None:
            w.kill()
    first_chunk_mtime = (tdir / "chunk_0000.json").stat().st_mtime_ns
    audio_blocks = {p.name: p.stat().st_mtime_ns for p in (paths.source_work(sid) / "audio").glob("block_*.json")}
    assert db.job(jid)["status"] == "running"  # left behind by the killed worker
    w = _worker(paths.home)
    try:
        deadline = time.time() + 600
        while time.time() < deadline and db.job(jid)["status"] in ("queued", "running"):
            time.sleep(0.5)
    finally:
        (paths.home / f"worker.stop.{w.pid}").touch()
        w.wait(timeout=60)
    job = db.job(jid)
    assert job["status"] == "done", job["error"]
    assert job["crash_count"] == 1
    assert (tdir / "chunk_0000.json").stat().st_mtime_ns == first_chunk_mtime
    for name, m in audio_blocks.items():
        assert (paths.source_work(sid) / "audio" / name).stat().st_mtime_ns == m
    import json
    tr = json.loads((tdir / "transcript.json").read_text())
    words = tr["words"]
    starts = [w["start"] for w in words]
    assert starts == sorted(starts)
    for t, _text in SENTENCES:
        near = [w for w in words if t - 1 <= w["start"] <= t + 8]
        said = " ".join(w["text"].strip().lower().strip(".,") for w in near)
        assert said.count("boundary") == 1, said          # kept once, not twice, not lost
        assert abs(near[0]["start"] - t) < 0.8, (near[0], t)  # absolute time survives chunking

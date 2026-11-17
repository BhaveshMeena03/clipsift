import time

import pytest
from PIL import Image, ImageDraw, ImageFont

from tests import synth
from tests.conftest import ffmpeg, make_home, needs_media, needs_models, run_job

pytestmark = [needs_media, needs_models]


def test_reference_analysis_reads_style(tmp_path):
    if not synth.have_tts():
        pytest.skip("needs macOS TTS")
    wav = synth.tts("bro what are you doing hahaha that was so funny", "Rishi", tmp_path / "s.wav")
    cap = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(cap)
    f = ImageFont.truetype(str(synth.FONT), 72)
    d.text((300, int(1920 * 0.70)), "BRO WHAT", font=f, fill="white", stroke_width=6, stroke_fill="black")
    cap.save(tmp_path / "cap.png")
    ref = tmp_path / "my_reel.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=8", "-loop", "1", "-i", str(tmp_path / "cap.png"),
           "-i", str(wav), "-filter_complex", "[0:v][1:v]overlay=0:0:shortest=1[v];[2:a]apad=whole_dur=8[a]",
           "-map", "[v]", "-map", "[a]", "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(ref))
    paths, db = make_home(tmp_path)
    rid = db.insert("refs", path=str(ref), title="my reel", views=12000, created_at=time.time())
    job = run_job(paths, db, "reference", "heavy", payload={"ref_ids": [rid]})
    assert job["status"] == "done", job["error"]
    feats = db.one("SELECT * FROM refs WHERE id=?", (rid,))["features"]
    assert feats["duration"] == pytest.approx(8.0, abs=0.1) and feats["orientation"] == "vertical"
    assert feats["captions"]["seen"] and feats["captions"]["upper_share"] > 0.9
    assert 0.6 < feats["captions"]["y"] < 0.8
    assert feats["words_per_s"] > 0.5
    assert feats["category"] in ("funny", "other")

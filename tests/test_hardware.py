"""Hardware choices for PCs we cannot test on directly: different NVIDIA cards, AMD, Intel, none."""
import subprocess

import pytest

from clipsift.analysis.asr import plan_asr
from clipsift.media import ffmpeg
from tests.conftest import ffmpeg as run_ffmpeg_cli
from tests.conftest import needs_media

ADA = {"float16", "int8_float16", "int8", "int8_float32", "float32", "bfloat16"}
PASCAL = {"int8", "int8_float32", "float32"}       # GTX 10-series: no float16


def gpu(i, name, free, total=12282):
    return {"index": i, "name": name, "free_mb": free, "total_mb": total, "driver": "566.03"}


@pytest.mark.parametrize("card,free,want", [
    ("NVIDIA GeForce RTX 4070 Ti", 11000, "float16"),
    ("NVIDIA GeForce RTX 4060", 7400, "float16"),
    ("NVIDIA GeForce RTX 4060 (busy with a game)", 2000, "int8_float16"),
])
def test_rtx_cards_use_the_gpu(card, free, want):
    p = plan_asr({}, None, n_cuda=1, gpus=[gpu(0, card, free)], types_fn=lambda i: ADA)
    assert (p.device, p.compute_type, p.device_index) == ("cuda", want, 0)
    assert card.split(" (")[0] in p.reason


def test_gtx_10_series_gets_int8_not_cpu():
    p = plan_asr({}, None, n_cuda=1, gpus=[gpu(0, "NVIDIA GeForce GTX 1060 6GB", 5800, 6144)], types_fn=lambda i: PASCAL)
    assert (p.device, p.compute_type) == ("cuda", "int8")
    assert "no float16" in p.reason


def test_full_gpu_falls_back_to_cpu_with_reason():
    p = plan_asr({}, None, n_cuda=1, gpus=[gpu(0, "NVIDIA GeForce RTX 4060", 900)], types_fn=lambda i: ADA)
    assert p.device == "cpu" and "900 MB free" in p.reason and p.model == "hinglish-swift"


def test_multi_gpu_uses_the_card_with_room():
    gpus = [gpu(0, "NVIDIA GeForce RTX 3060", 1200), gpu(1, "NVIDIA GeForce RTX 4070 Ti", 11500)]
    p = plan_asr({}, None, n_cuda=2, gpus=gpus, types_fn=lambda i: ADA)
    assert (p.device, p.device_index) == ("cuda", 1) and "4070 Ti" in p.reason


def test_no_cuda_amd_or_intel():
    p = plan_asr({}, None, n_cuda=0, gpus=[])
    assert p.device == "cpu" and "non-NVIDIA" in p.reason


def test_unknown_precisions_still_try_gpu():
    p = plan_asr({}, None, n_cuda=1, gpus=[gpu(0, "Future GPU", 20000)], types_fn=lambda i: set())
    assert (p.device, p.compute_type) == ("cuda", "float16")


def test_cpu_setting_wins():
    p = plan_asr({"asr_device": "cpu"}, None, n_cuda=1, gpus=[gpu(0, "RTX 4070 Ti", 11000)], types_fn=lambda i: ADA)
    assert p.device == "cpu"


def test_nvidia_smi_parsing(monkeypatch):
    from clipsift import gpu as gpumod
    monkeypatch.setattr(gpumod, "find_on_path", lambda name: "/usr/bin/nvidia-smi")
    out = "0, NVIDIA GeForce RTX 4070 Ti, 12282, 11020, 566.03\n1, NVIDIA GeForce RTX 4060, 8188, 7700, 566.03\n"
    monkeypatch.setattr(gpumod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    g = gpumod.nvidia_smi()
    assert [(x["index"], x["name"], x["free_mb"]) for x in g] == [(0, "NVIDIA GeForce RTX 4070 Ti", 11020),
                                                                  (1, "NVIDIA GeForce RTX 4060", 7700)]


# ---------- encoders ----------

@pytest.fixture
def fake_encoders(monkeypatch):
    """Simulate an ffmpeg build: which encoders are listed and which option sets the card accepts."""
    state = {"listed": set(), "works": set()}

    def fake_capture(cmd, timeout=120):
        if "-encoders" in cmd:
            lines = "\n".join(f" V....D {e}  x" for e in state["listed"])
            return subprocess.CompletedProcess(cmd, 0, stdout=lines.encode(), stderr=b"")
        codec = cmd[cmd.index("-c:v") + 1]
        variant = "max" if ("-rc-lookahead" in cmd or "-quality" in cmd or "veryslow" in cmd) else "basic"
        ok = (codec, variant) in state["works"]
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(ffmpeg, "run_capture", fake_capture)
    monkeypatch.setattr(ffmpeg, "ffmpeg_bin", lambda: "ffmpeg")
    ffmpeg._encoder_cache.clear()
    monkeypatch.setattr(ffmpeg, "_listed", None)
    yield state
    ffmpeg._encoder_cache.clear()
    ffmpeg._listed = None


def test_rtx_gets_max_quality_nvenc(fake_encoders):
    fake_encoders["listed"] = {"h264_nvenc", "h264_amf", "h264_qsv", "libx264"}
    fake_encoders["works"] = {("h264_nvenc", "max"), ("h264_nvenc", "basic")}
    assert ffmpeg.pick_hw_encoder() == ("nvenc", 0)
    assert ffmpeg.hw_encoder_label() == "NVIDIA NVENC"


def test_old_driver_gets_plain_nvenc(fake_encoders):
    fake_encoders["listed"] = {"h264_nvenc"}
    fake_encoders["works"] = {("h264_nvenc", "basic")}
    assert ffmpeg.pick_hw_encoder() == ("nvenc", 1)


@pytest.mark.parametrize("works,want", [({("h264_amf", "max")}, ("amf", 0)), ({("h264_qsv", "basic")}, ("qsv", 1))])
def test_amd_and_intel(fake_encoders, works, want):
    fake_encoders["listed"] = {"h264_nvenc", "h264_amf", "h264_qsv"}   # gyan builds list all three
    fake_encoders["works"] = works                                      # only the real card answers
    assert ffmpeg.pick_hw_encoder() == want


def test_nothing_works_means_x264(fake_encoders):
    fake_encoders["listed"] = {"h264_nvenc", "h264_amf", "h264_qsv"}
    assert ffmpeg.pick_hw_encoder() is None
    assert ffmpeg.pick_hw_encoder("x264") is None


def test_bad_encoder_is_retired(fake_encoders):
    fake_encoders["listed"] = {"h264_nvenc", "h264_qsv"}
    fake_encoders["works"] = {("h264_nvenc", "max"), ("h264_nvenc", "basic"), ("h264_qsv", "max")}
    ffmpeg.mark_encoder_bad("nvenc", 0)
    assert ffmpeg.pick_hw_encoder() == ("nvenc", 1)
    ffmpeg.mark_encoder_bad("nvenc", 1)
    assert ffmpeg.pick_hw_encoder() == ("qsv", 0)


def test_encoder_args_shape():
    for name in ffmpeg.HW_ENCODERS:
        for v in range(2):
            a = ffmpeg.encoder_args(name, v)
            assert a[0] == "-c:v" and a[1] == ffmpeg.HW_ENCODERS[name]["codec"]
            assert a[-2] == "-pix_fmt" and "{q" not in " ".join(a)
    assert "nv12" in ffmpeg.encoder_args("qsv", 0)   # Quick Sync does not take yuv420p


@needs_media
def test_render_survives_a_hardware_encoder_that_breaks(tmp_path, monkeypatch):
    """Passes the test encode, then fails on the real clip: the export must still finish on x264."""
    from clipsift.render import render as R
    src = tmp_path / "in.mp4"
    run_ffmpeg_cli("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=3", "-f", "lavfi", "-i", "sine=d=3",
                   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src))
    calls = []
    retired = []

    def fake_pick(prefer="auto", fast=False):
        calls.append(prefer)
        return None if retired else ("nvenc", 0)

    monkeypatch.setattr(R, "pick_hw_encoder", fake_pick)
    monkeypatch.setattr(R, "encoder_args", lambda n, v, fast=False: ["-c:v", "h264_not_a_real_encoder", "-pix_fmt", "yuv420p"])
    monkeypatch.setattr(R, "mark_encoder_bad", lambda n, v: retired.append((n, v)))
    spec = R.RenderSpec(src=src, width=1280, height=720, fps=30, orientation="horizontal", start=0.0, end=3.0,
                        layout={"gameplay_mode": "full", "brand_on": True, "brand": {"text": "x"}},
                        export_tracks=[0], mute_tracks=[0], loudnorm=False)
    info = R.render(spec, tmp_path / "out.mp4", tmp_path / "work")
    assert retired == [("nvenc", 0)] and len(calls) == 2
    assert (info["width"], info["height"]) == (1080, 1920)

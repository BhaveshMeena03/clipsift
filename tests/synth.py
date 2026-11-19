"""Build a labelled synthetic 'stream' for pipeline tests.

It checks mechanics (timestamps, OCR of a HUD, speech, export), not real gameplay understanding.
Speech uses macOS 'say' or the Windows speech engine; where neither works, speech tests are skipped."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).resolve().parents[1] / "clipsift" / "assets" / "fonts" / "Poppins-Bold.ttf"


_sapi_ok: bool | None = None
_SAPI = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.SetOutputToWaveFile($env:CS_TTS_OUT); $s.Speak($env:CS_TTS_TEXT); $s.Dispose()")


def _sapi_say(text: str, wav: Path) -> None:
    """Windows' built-in speech engine. Text and path go through env vars, no quoting issues."""
    env = dict(os.environ, CS_TTS_OUT=str(wav), CS_TTS_TEXT=text)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI], env=env, check=True,
                   capture_output=True, timeout=120)


def have_tts() -> bool:
    global _sapi_ok
    if shutil.which("say"):
        return True
    if sys.platform != "win32":
        return False
    if _sapi_ok is None:
        try:
            probe = Path(tempfile.mkdtemp()) / "probe.wav"
            _sapi_say("test", probe)
            _sapi_ok = probe.is_file() and probe.stat().st_size > 2000
        except Exception:
            _sapi_ok = False
    return _sapi_ok


def tts(text: str, voice: str, out: Path, fallback: str | None = None) -> Path:
    """macOS 'say' with the named voice, or the default Windows voice. Windows has no Hindi voice by
    default, so Hindi lines pass a romanized fallback."""
    raw = out.with_name(out.stem + ".raw" + (".aiff" if shutil.which("say") else ".wav"))
    if shutil.which("say"):
        subprocess.run(["say", "-v", voice, "-o", str(raw), text], check=True)
    else:
        _sapi_say(fallback or text, raw)
    # trim leading silence so the speech starts exactly where the test puts it
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-af",
                    "silenceremove=start_periods=1:start_threshold=-50dB", "-ar", "48000", "-ac", "1", str(out)],
                   check=True)
    raw.unlink()
    return out


def killfeed_png(rows: list[tuple[str, str]], out: Path) -> Path:
    img = Image.new("RGBA", (540, 40 * len(rows) + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(str(FONT), 22)
    for i, (a, b) in enumerate(rows):
        y = 5 + i * 40
        d.rectangle([0, y, 539, y + 34], fill=(10, 10, 10, 200))
        d.text((12, y + 3), a, font=f, fill=(240, 240, 240, 255))
        d.rectangle([230, y + 10, 300, y + 24], fill=(220, 220, 220, 255))  # weapon icon
        d.text((320, y + 3), b, font=f, fill=(240, 240, 240, 255))
    img.save(out)
    return out


def banner_png(text: str, out: Path) -> Path:
    img = Image.new("RGBA", (700, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(str(FONT), 96)
    w = d.textlength(text, font=f)
    d.text(((700 - w) / 2, 5), text, font=f, fill=(255, 230, 120, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))
    img.save(out)
    return out


def build(out_dir: Path, duration: float = 240.0, extra_tracks: bool = False) -> dict:
    """Returns {'path', 'labels'}; labels are the ground truth for eval tests."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "synthetic_stream.mp4"
    labels_path = out_dir / "labels.json"
    if path.exists() and labels_path.exists():
        return {"path": path, "labels": json.loads(labels_path.read_text())}
    assets = out_dir / "assets"
    assets.mkdir(exist_ok=True)
    speech = []
    if have_tts():
        speech = [
            (40.0, tts("bro what are you doing? hahaha. oh my god, why would you jump there", "Rishi", assets / "s1.wav"), 0.0),
            (46.0, tts("भाई क्या कर रहा है, पागल है क्या", "Lekha", assets / "s2.wav",
                       fallback="bhai kya kar raha hai, pagal hai kya"), 0.0),
            (190.0, tts("testing the microphone one two three, testing testing", "Rishi", assets / "s3.wav"), 9.0),
        ]
    kf1 = killfeed_png([("fortysevenfrags", "Raze_main")], assets / "kf1.png")
    kf2 = killfeed_png([("fortysevenfrags", "Raze_main"), ("fortysevenfrags", "jett_diff")], assets / "kf2.png")
    kf3 = killfeed_png([("fortysevenfrags", "Raze_main"), ("fortysevenfrags", "jett_diff"),
                        ("fortysevenfrags", "sova123")], assets / "kf3.png")
    ban = banner_png("CLUTCH", assets / "banner.png")
    d = duration
    inputs = ["-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={d}",
              "-f", "lavfi", "-i", "cellauto=s=480x270:rule=110:r=30,scale=480:270",
              "-loop", "1", "-i", str(kf1), "-loop", "1", "-i", str(kf2), "-loop", "1", "-i", str(kf3),
              "-loop", "1", "-i", str(ban),
              "-f", "lavfi", "-i", f"anoisesrc=d={d}:c=pink:r=48000:a=0.004"]
    for _t, wav, _g in speech:
        inputs += ["-i", str(wav)]
    v = ("[0:v][1:v]overlay=40:40:shortest=1[a];"
         "[a][2:v]overlay=1360:90:enable='between(t,120,123)'[b];"
         "[b][3:v]overlay=1360:90:enable='between(t,123,126)'[c];"
         "[c][4:v]overlay=1360:90:enable='between(t,126,131)'[e];"
         "[e][5:v]overlay=610:150:enable='between(t,131,135)',format=yuv420p[vout]")
    a_parts = ["[6:a]volume=1[bg]"]
    mix = ["[bg]"]
    for i, (t, _wav, gain) in enumerate(speech):
        idx = 7 + i
        ms = int(t * 1000)
        a_parts.append(f"[{idx}:a]volume={gain}dB,adelay={ms}|{ms},apad=whole_dur={d}[sp{i}]")
        mix.append(f"[sp{i}]")
    a_parts.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first,atrim=0:{d}[aout]")
    graph = v + ";" + ";".join(a_parts)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
           "-map", "[vout]", "-map", "[aout]", "-t", str(d), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
           "-g", "300", "-c:a", "aac", "-b:a", "128k"]
    if extra_tracks:
        cmd += ["-map", "6:a", "-c:a:1", "aac"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    labels = {
        "source": path.name,
        "tolerance_s": 10,
        "moments": [
            {"t": 45.0, "category": "funny", "note": "spoken funny moment, English + Hindi",
             "requires_tts": True},
            {"t": 128.0, "category": "skill", "note": "quiet 3 kills + CLUTCH banner, no voice, no gunfire"},
        ],
        "not_highlights": [{"t": 195.0, "note": "loud mic test, should not rank as a highlight", "requires_tts": True}],
        "facecam": [40 / 1920, 40 / 1080, 480 / 1920, 270 / 1080],
    }
    labels_path.write_text(json.dumps(labels, indent=1))
    return {"path": path, "labels": labels}


if __name__ == "__main__":
    import sys
    r = build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/clipsift_synth"))
    print(r["path"])

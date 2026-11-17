"""Model registry and downloads. Sizes are download sizes; licenses are the model weights'."""
import logging
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

WHISPER = {
    # Oriserve fine-tunes write Hindi and English speech as Roman Hinglish. They ship as
    # transformers checkpoints, so setup converts them once (pinned commit) to CTranslate2.
    "hinglish-apex": {"repo": "Oriserve/Whisper-Hindi2Hinglish-Apex", "revision": "f3214eed20b4e4d4144e739982d911f87b9cb223",
                      "mb": 1620, "license": "Apache-2.0", "convert": True, "family": "hinglish",
                      "note": "GPU default: Hindi/English/mixed speech as Roman Hinglish (large-v3-turbo based)"},
    "hinglish-swift": {"repo": "Oriserve/Whisper-Hindi2Hinglish-Swift", "revision": "cd0d4d013062d542569135460a79025337df1982",
                       "mb": 290, "license": "Apache-2.0", "convert": True, "family": "hinglish",
                       "note": "CPU default: small Hinglish model (whisper-base based), less accurate"},
    "tiny": {"repo": "Systran/faster-whisper-tiny", "mb": 75, "license": "MIT", "note": "tests only"},
    "small": {"repo": "Systran/faster-whisper-small", "mb": 484, "license": "MIT", "note": "stock Whisper, CPU"},
    "medium": {"repo": "Systran/faster-whisper-medium", "mb": 1530, "license": "MIT", "note": "slower CPU option"},
    "large-v2": {"repo": "Systran/faster-whisper-large-v2", "mb": 3090, "license": "MIT", "note": "GPU, fewer hallucinations on some audio"},
    "large-v3": {"repo": "Systran/faster-whisper-large-v3", "mb": 3090, "license": "MIT", "note": "stock Whisper, writes Hindi in Devanagari (romanized after)"},
    "large-v3-turbo": {"repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "mb": 1620, "license": "MIT", "note": "GPU, about 3x faster, a bit weaker on Hindi"},
}

TAGGER = {
    "ced-mini": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/"
               "sherpa-onnx-ced-mini-audio-tagging-2024-04-19.tar.bz2",
        "dir": "sherpa-onnx-ced-mini-audio-tagging-2024-04-19",
        "mb": 48, "license": "Apache-2.0",
    },
}

# rough VRAM needs in MB: (float16, int8_float16)
VRAM_NEED = {
    "hinglish-apex": (2600, 1800), "hinglish-swift": (500, 400),
    "tiny": (600, 500), "small": (1300, 900), "medium": (2700, 1700),
    "large-v2": (4300, 2900), "large-v3": (4300, 2900), "large-v3-turbo": (2600, 1800),
}


def whisper_dir(models_dir: Path, name: str) -> Path:
    return Path(models_dir) / "whisper" / name


def whisper_ready(models_dir: Path, name: str) -> bool:
    d = whisper_dir(models_dir, name)
    return (d / "model.bin").is_file() and (d / "config.json").is_file()


def tagger_dir(models_dir: Path, name: str = "ced-mini") -> Path:
    return Path(models_dir) / "tagger" / TAGGER[name]["dir"]


def tagger_ready(models_dir: Path, name: str = "ced-mini") -> bool:
    d = tagger_dir(models_dir, name)
    return (d / "model.int8.onnx").is_file() and (d / "class_labels_indices.csv").is_file()


def family(name: str) -> str:
    return WHISPER.get(name, {}).get("family", "whisper")


def download_whisper(models_dir: Path, name: str) -> Path:
    if name not in WHISPER:
        raise ValueError(f"unknown model {name}")
    if WHISPER[name].get("convert"):
        raise RuntimeError(f"{name} needs conversion; run setup\\convert_models.ps1 (see docs/MODELS.md)")
    from huggingface_hub import snapshot_download
    target = whisper_dir(models_dir, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot_download(WHISPER[name]["repo"], local_dir=str(target),
                      allow_patterns=["*.bin", "*.json", "*.txt", "vocabulary.*"])
    if not whisper_ready(models_dir, name):
        raise RuntimeError(f"download of {name} looks incomplete")
    return target


def download_tagger(models_dir: Path, name: str = "ced-mini") -> Path:
    info = TAGGER[name]
    root = Path(models_dir) / "tagger"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        arc = Path(tmp) / "m.tar.bz2"
        with urllib.request.urlopen(info["url"], timeout=60) as r, open(arc, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        with tarfile.open(arc, "r:bz2") as tf:
            safe = [m for m in tf.getmembers()
                    if not (m.name.startswith("/") or ".." in Path(m.name).parts) and (m.isfile() or m.isdir())]
            tf.extractall(tmp, members=safe)
        src = Path(tmp) / info["dir"]
        dst = root / info["dir"]
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
        # the test wavs and the fp32 model are not needed
        shutil.rmtree(dst / "test_wavs", ignore_errors=True)
        (dst / "model.onnx").unlink(missing_ok=True)
    if not tagger_ready(models_dir, name):
        raise RuntimeError("tagger download looks incomplete")
    return dst


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    from .config import Paths
    ap = argparse.ArgumentParser(prog="python -m clipsift.models")
    ap.add_argument("--home", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    d = sub.add_parser("download")
    d.add_argument("name")
    sub.add_parser("convert-list", help="repo and revision of models that need conversion (used by setup)")
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else None).ensure()
    if args.cmd == "status":
        for name, info in WHISPER.items():
            state = "ready" if whisper_ready(paths.models, name) else ("needs conversion" if info.get("convert") else "not downloaded")
            print(f"{name:16} {info['mb']:>6} MB  {info['license']:10} {state:16} {info['note']}")
        print(f"{'ced-mini':16} {TAGGER['ced-mini']['mb']:>6} MB  {'Apache-2.0':10} "
              f"{'ready' if tagger_ready(paths.models) else 'not downloaded':16} laughter / scream / gunshot tagger")
        return 0
    if args.cmd == "convert-list":
        print(json.dumps({k: {"repo": v["repo"], "revision": v["revision"],
                              "out": str(whisper_dir(paths.models, k))} for k, v in WHISPER.items() if v.get("convert")}))
        return 0
    if args.name == "ced-mini":
        print(download_tagger(paths.models))
    else:
        print(download_whisper(paths.models, args.name))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

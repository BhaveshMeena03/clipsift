"""Model registry and downloads. Sizes are download sizes; licenses are the model weights'."""
import hashlib
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
    # stock models, each pinned to the commit that was checked (19 Nov 2026)
    "tiny": {"repo": "Systran/faster-whisper-tiny", "revision": "d90ca5fe260221311c53c58e660288d3deb8d356",
             "mb": 75, "license": "MIT", "note": "tests only"},
    "small": {"repo": "Systran/faster-whisper-small", "revision": "536b0662742c02347bc0e980a01041f333bce120",
              "mb": 484, "license": "MIT", "note": "stock Whisper, CPU"},
    "medium": {"repo": "Systran/faster-whisper-medium", "revision": "08e178d48790749d25932bbc082711ddcfdfbc4f",
               "mb": 1530, "license": "MIT", "note": "slower CPU option"},
    "large-v2": {"repo": "Systran/faster-whisper-large-v2", "revision": "f0fe81560cb8b68660e564f55dd99207059c092e",
                 "mb": 3090, "license": "MIT", "note": "GPU, fewer hallucinations on some audio"},
    "large-v3": {"repo": "Systran/faster-whisper-large-v3", "revision": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
                 "mb": 3090, "license": "MIT", "note": "stock Whisper, writes Hindi in Devanagari (romanized after)"},
    # moved from mobiuslabsgmbh; model files unchanged since Oct 2024, later commits only touched the README
    "large-v3-turbo": {"repo": "dropbox-dash/faster-whisper-large-v3-turbo",
                       "revision": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
                       "mb": 1620, "license": "MIT", "note": "GPU, about 3x faster, a bit weaker on Hindi"},
}

TAGGER = {
    "ced-mini": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/"
               "sherpa-onnx-ced-mini-audio-tagging-2024-04-19.tar.bz2",
        "dir": "sherpa-onnx-ced-mini-audio-tagging-2024-04-19",
        "mb": 48, "license": "Apache-2.0",
        # the release has no published checksum, so the files ClipSift uses are checked after unpacking
        "sha256": {"model.int8.onnx": "ff29f39f9fbe637f72535160e9d006d61d872fdab0fce838672265b9b38cf946",
                   "class_labels_indices.csv": "cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429"},
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
    snapshot_download(WHISPER[name]["repo"], revision=WHISPER[name]["revision"], local_dir=str(target),
                      allow_patterns=["*.bin", "*.json", "*.txt", "vocabulary.*"])
    if not whisper_ready(models_dir, name):
        raise RuntimeError(f"download of {name} looks incomplete")
    return target


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def unpack(archive: Path, dest: Path) -> None:
    """Plain files and folders only, and nothing outside dest: the 'data' filter refuses absolute paths
    (including Windows drive paths like C:/...), '..', links and device files."""
    with tarfile.open(archive, "r:*") as tf:
        tf.extractall(dest, members=[m for m in tf.getmembers() if m.isfile() or m.isdir()], filter="data")


def download_tagger(models_dir: Path, name: str = "ced-mini") -> Path:
    info = TAGGER[name]
    root = Path(models_dir) / "tagger"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        arc = Path(tmp) / "m.tar.bz2"
        with urllib.request.urlopen(info["url"], timeout=60) as r, open(arc, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        unpack(arc, Path(tmp))
        src = Path(tmp) / info["dir"]
        for fname, want in info["sha256"].items():
            if sha256_file(src / fname) != want:
                raise RuntimeError(f"{fname} from the {name} download does not match its pinned checksum; not using it")
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

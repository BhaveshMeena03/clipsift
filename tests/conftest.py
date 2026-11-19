import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from clipsift import models as _models
from clipsift.config import Paths as _Paths

_APP = _Paths()  # the installed app's data folder (data\ next to the app on Windows)


def _pick_models() -> Path:
    if os.environ.get("CLIPSIFT_TEST_MODELS"):
        return Path(os.environ["CLIPSIFT_TEST_MODELS"])
    for cand in (Path.home() / ".clipsift-dev" / "models", _APP.home / "models"):
        if _models.whisper_ready(cand, "hinglish-swift"):
            return cand
    return _APP.home / "models"


MODELS = _pick_models()
os.environ["CLIPSIFT_MODELS"] = str(MODELS)  # every test home and worker uses these models
SYNTH_DIR = Path(os.environ.get("CLIPSIFT_TEST_MEDIA", Path(tempfile.gettempdir()) / "clipsift_synth"))

# setup puts ffmpeg in the data folder, not on PATH; tests call it by name
_ff_bin = _APP.tools / "ffmpeg" / "bin"
if (_ff_bin / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")).is_file():
    os.environ["PATH"] = str(_ff_bin) + os.pathsep + os.environ.get("PATH", "")


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def have_models() -> bool:
    return _models.whisper_ready(MODELS, "hinglish-swift") and _models.tagger_ready(MODELS)


needs_media = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
needs_models = pytest.mark.skipif(not have_models(), reason=f"models not found in {MODELS}")


def make_home(root: Path, **settings):
    from clipsift.config import Paths
    from clipsift.db import DB
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    paths = Paths(home).ensure()
    db = DB(paths.db)
    base = {"asr_device": "cpu", "asr_model_cpu": "hinglish-swift", "max_candidates": 10}
    base.update(settings)
    db.set_settings(base)
    return paths, db


def add_source(db, path: Path, game="generic", layout=None, **kw) -> int:
    lid = None
    if layout:
        lid = db.insert("layouts", name=f"l{time.time_ns()}", data_json=layout, created_at=time.time(), updated_at=time.time())
    return db.insert("sources", kind="local", path=str(path), title=path.stem, game=game, layout_id=lid,
                     created_at=time.time(), updated_at=time.time(), **kw)


def run_job(paths, db, kind, lane, **kw) -> dict:
    from clipsift.worker import Worker
    jid = db.add_job(kind, lane, **kw)
    w = Worker(paths)
    job = db.claim_job(lane, os.getpid())
    assert job and job["id"] == jid
    w.run_job(job)
    return db.job(jid)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

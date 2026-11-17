import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

MODELS = Path(os.environ.get("CLIPSIFT_TEST_MODELS", Path.home() / ".clipsift-dev" / "models"))
SYNTH_DIR = Path(os.environ.get("CLIPSIFT_TEST_MEDIA", "/tmp/clipsift_synth"))


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def have_models() -> bool:
    from clipsift import models
    return models.whisper_ready(MODELS, "hinglish-swift") and models.tagger_ready(MODELS)


needs_media = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
needs_models = pytest.mark.skipif(not have_models(), reason=f"models not found in {MODELS}")


def make_home(root: Path, **settings):
    from clipsift.config import Paths
    from clipsift.db import DB
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    if not (home / "models").exists():
        os.symlink(MODELS, home / "models", target_is_directory=True)
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

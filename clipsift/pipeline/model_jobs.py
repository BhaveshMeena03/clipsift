import threading
import time

from .. import models
from ..util import dir_size, human_bytes


def run_model_download(ctx) -> dict:
    name = (ctx.job.get("payload") or {}).get("name")
    if name == "ced-mini":
        target, size_mb, fn = models.tagger_dir(ctx.paths.models), models.TAGGER["ced-mini"]["mb"], \
            lambda: models.download_tagger(ctx.paths.models)
    else:
        target, size_mb, fn = models.whisper_dir(ctx.paths.models, name), models.WHISPER[name]["mb"], \
            lambda: models.download_whisper(ctx.paths.models, name)
    result: dict = {}

    def work():
        try:
            result["path"] = str(fn())
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    root = target.parent
    while t.is_alive():
        got = dir_size(root) / 2**20
        ctx.progress("models", min(0.99, got / max(1, size_mb)), f"Downloading {name}: {human_bytes(got * 2**20)} "
                                                                     f"of about {size_mb} MB")
        if ctx.cancelled():
            return {"message": "Cancelled; the download keeps going in the background until it finishes."}
        time.sleep(1.0)
    if "error" in result:
        raise RuntimeError(f"Model download failed: {result['error']}")
    return {"message": f"{name} is ready", "path": result["path"]}

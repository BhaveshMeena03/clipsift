"""Background worker. One thread per lane: heavy (one model job at a time), light (previews and
exports), download. Started and watched by the server; safe to kill, jobs resume on restart."""
import argparse
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from .config import IS_WINDOWS, Paths
from .db import DB
from .log import register_secret, setup_logging
from .media import ffmpeg
from .media.disk import DiskSpaceError
from .pipeline.context import JobContext, Paused
from .util import Cancelled

log = logging.getLogger("clipsift.worker")

LANES = ("heavy", "light", "download")


def handler_for(kind: str):
    if kind == "process":
        from .pipeline.process import STAGES, run_process
        return run_process, STAGES
    if kind == "preview":
        from .pipeline.export import run_preview
        return run_preview, [("render", 1)]
    if kind == "export":
        from .pipeline.export import run_export
        return run_export, [("render", 1)]
    if kind == "download":
        from .ingest.youtube import run_download
        return run_download, [("download", 1)]
    if kind == "reference":
        from .prefs.references import run_reference_analysis
        return run_reference_analysis, [("reference", 1)]
    if kind == "models":
        from .pipeline.model_jobs import run_model_download
        return run_model_download, [("models", 1)]
    raise ValueError(f"unknown job kind {kind}")


class KeepAwake:
    """Stop Windows from sleeping while a job runs (display can still turn off). The flag is
    per thread, so each lane sets and clears its own. No-op on other systems."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


def friendly_error(e: BaseException) -> str:
    if isinstance(e, DiskSpaceError):
        return str(e)
    if isinstance(e, MemoryError):
        return "Ran out of memory. Close other programs or use a smaller speech model."
    msg = str(e).strip() or e.__class__.__name__
    return msg[:1500]


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True


class Worker:
    def __init__(self, paths: Paths):
        self.paths = paths.ensure()
        self.db = DB(paths.db)
        self.stop = threading.Event()
        self.pid = os.getpid()

    def configure(self, settings: dict) -> None:
        ffmpeg.configure(self.paths.tools, settings.get("ffmpeg_path", ""), settings.get("ffprobe_path", ""))
        register_secret(settings.get("instagram_graph_token", ""))

    def run_job(self, job: dict) -> None:
        settings = self.db.settings()
        self.configure(settings)
        func, stages = handler_for(job["kind"])
        ctx = JobContext(self.db, self.paths, job, settings, self.stop, stages)
        log.info("job %s (%s) started", job["id"], job["kind"])
        try:
            with KeepAwake():
                result = func(ctx) or {}
            msg = result.get("message") or "Done"
            self.db.update("jobs", job["id"], status="done", progress=1.0, result_json=result, message=msg,
                           eta_s=None, finished_at=time.time(), updated_at=time.time())
            log.info("job %s done", job["id"])
        except Cancelled:
            self.db.update("jobs", job["id"], status="cancelled", eta_s=None, finished_at=time.time(),
                           message="Cancelled. Finished parts are kept, so Resume picks up where it stopped.")
            self._source_status(job, "cancelled")
        except Paused:
            self.db.update("jobs", job["id"], status="queued", eta_s=None, updated_at=time.time(),
                           message="Paused because the app closed. It resumes on next start.")
        except Exception as e:
            log.error("job %s failed: %s\n%s", job["id"], e, traceback.format_exc())
            self.db.update("jobs", job["id"], status="failed", error=friendly_error(e), eta_s=None,
                           finished_at=time.time(), message="Failed")
            self._source_status(job, "failed", friendly_error(e))

    def _source_status(self, job, status, error=None):
        if job["kind"] in ("process", "download") and job.get("source_id"):
            self.db.update("sources", job["source_id"], status=status, error=error, updated_at=time.time())

    def lane(self, lane: str) -> None:
        while not self.stop.is_set():
            try:
                job = self.db.claim_job(lane, self.pid)
            except Exception as e:
                log.warning("claim failed: %s", e)
                job = None
            if job is None:
                self.stop.wait(0.5)
                continue
            self.run_job(job)

    def serve(self, stop_file: Path | None = None, parent_pid: int | None = None) -> int:
        lock = SingleInstance(self.paths.lock)
        if not lock.acquire():
            log.error("another worker is already running")
            return 2
        back = self.db.requeue_interrupted()
        if back:
            log.info("resuming jobs %s", back)
        threads = [threading.Thread(target=self.lane, args=(ln,), name=f"lane-{ln}", daemon=True) for ln in LANES]
        for t in threads:
            t.start()
        parent = _parent_handle(parent_pid)
        last_parent_check = 0.0
        try:
            while not self.stop.is_set():
                if stop_file and stop_file.exists():
                    log.info("stop requested")
                    break
                if parent is not None and time.time() - last_parent_check > 2:
                    last_parent_check = time.time()
                    if not _alive(parent):
                        log.warning("the app window closed without stopping the worker; pausing jobs")
                        break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        self.stop.set()
        for t in threads:
            t.join(timeout=60)
        # anything still marked running by us was interrupted on purpose
        for j in self.db.all("SELECT id FROM jobs WHERE status='running' AND worker_pid=?", (self.pid,)):
            self.db.update("jobs", j["id"], status="queued", message="Paused because the app closed.")
        if stop_file:
            stop_file.unlink(missing_ok=True)
        return 0


def _parent_handle(pid: int | None):
    if not pid:
        return None
    import psutil
    try:
        p = psutil.Process(pid)
        return (p, p.create_time())
    except psutil.Error:
        return None


def _alive(handle) -> bool:
    import psutil
    proc, created = handle
    try:
        return proc.is_running() and proc.create_time() == created and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None)
    ap.add_argument("--parent-pid", type=int, default=None)
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else None).ensure()
    setup_logging(paths.logs, "worker")
    w = Worker(paths)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    stop_file = paths.home / f"worker.stop.{os.getpid()}"
    stop_file.unlink(missing_ok=True)
    return w.serve(stop_file, args.parent_pid)


if __name__ == "__main__":
    sys.exit(main())

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from ..util import Cancelled, atomic_write_json, read_json

log = logging.getLogger(__name__)


class Paused(Exception):
    """App is closing; the job goes back to the queue and resumes next start."""


class JobContext:
    def __init__(self, db, paths, job: dict, settings: dict, stop_event: threading.Event | None = None,
                 stages: list[tuple[str, float]] | None = None):
        self.db = db
        self.paths = paths
        self.job = job
        self.id = job["id"]
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.stages = stages or []
        self._total_w = sum(w for _s, w in self.stages) or 1.0
        self._stage = ""
        self._last_check = 0.0
        self._cancel = False
        self._last_write = 0.0
        self._t0 = time.time()
        self._stage_t0 = time.time()
        self.timings: dict[str, float] = {}
        self.notes: list[str] = []

    def cancelled(self) -> bool:
        if self.stop_event.is_set():
            raise Paused()
        now = time.time()
        if now - self._last_check > 0.5:
            self._last_check = now
            row = self.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,))
            self._cancel = bool(row and row["cancel_requested"])
        return self._cancel

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def overall(self, stage: str, frac: float) -> float:
        done = 0.0
        for s, w in self.stages:
            if s == stage:
                return min(1.0, (done + w * max(0.0, min(1.0, frac))) / self._total_w)
            done += w
        return 0.0

    def progress(self, stage: str, frac: float, message: str | None = None, force: bool = False) -> None:
        now = time.time()
        if stage != self._stage:
            if self._stage:
                self.timings[self._stage] = round(self.timings.get(self._stage, 0.0) + now - self._stage_t0, 2)
            self._stage = stage
            self._stage_t0 = now
            force = True
        if not force and now - self._last_write < 0.5:
            return
        self._last_write = now
        overall = self.overall(stage, frac) if self.stages else frac
        eta = None
        elapsed = now - self._t0
        if overall > 0.02 and elapsed > 20:
            eta = elapsed * (1 - overall) / overall
        fields = {"stage": stage, "progress": round(overall, 4), "eta_s": eta, "updated_at": now}
        if message is not None:
            fields["message"] = message
        self.db.update("jobs", self.id, **fields)

    def finish_timings(self) -> dict[str, float]:
        if self._stage:
            self.timings[self._stage] = round(self.timings.get(self._stage, 0.0) + time.time() - self._stage_t0, 2)
            self._stage = ""
        return dict(self.timings)

    def note(self, text: str) -> None:
        log.info("job %s: %s", self.id, text)
        self.notes.append(text)
        self.db.update("jobs", self.id, message=text, updated_at=time.time())


def stage_dir(root: Path, name: str, params: dict) -> Path:
    """A folder of stage outputs that is wiped when the inputs or settings change."""
    params = json.loads(json.dumps(params, sort_keys=True, default=str))  # tuples -> lists, like on disk
    d = Path(root) / name
    meta = d / "_params.json"
    old = read_json(meta)
    if old is not None and old != params:
        log.info("settings changed for %s, redoing it", name)
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    if read_json(meta) != params:
        atomic_write_json(meta, params)
    return d

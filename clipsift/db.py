import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import DEFAULT_SETTINGS

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,              -- local | youtube
  path TEXT,
  url TEXT,
  video_id TEXT,
  title TEXT,
  duration REAL,
  width INTEGER, height INTEGER, fps REAL,
  orientation TEXT,
  info_json TEXT DEFAULT '{}',
  track_roles_json TEXT DEFAULT '{}',
  game TEXT DEFAULT 'generic',
  layout_id INTEGER,
  options_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'new',
  error TEXT,
  fingerprint TEXT,
  recorded_at REAL,
  event_offset REAL,
  created_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,              -- process | download | preview | export | proxy | reference | verify
  lane TEXT NOT NULL,              -- heavy | light | download
  source_id INTEGER,
  candidate_id INTEGER,
  payload_json TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | cancelled
  stage TEXT DEFAULT '',
  progress REAL DEFAULT 0,
  message TEXT DEFAULT '',
  eta_s REAL,
  cancel_requested INTEGER DEFAULT 0,
  crash_count INTEGER DEFAULT 0,
  error TEXT,
  result_json TEXT DEFAULT '{}',
  worker_pid INTEGER,
  created_at REAL, started_at REAL, finished_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, lane);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL, peak REAL,
  category TEXT NOT NULL,
  score REAL DEFAULT 0,
  rank_score REAL DEFAULT 0,
  reason TEXT DEFAULT '',
  evidence_json TEXT DEFAULT '[]',
  features_json TEXT DEFAULT '{}',
  hooks_json TEXT DEFAULT '[]',
  captions_json TEXT DEFAULT '[]',
  edit_json TEXT DEFAULT '{}',
  status TEXT DEFAULT 'new',       -- new | accepted | rejected | exported
  proxy_path TEXT,
  proxy_start REAL,
  export_path TEXT,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS cand_source ON candidates(source_id);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER, source_id INTEGER,
  decision TEXT, category TEXT, features_json TEXT, at REAL
);

CREATE TABLE IF NOT EXISTS layouts (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  data_json TEXT NOT NULL,
  created_at REAL, updated_at REAL
);

CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY,
  path TEXT,
  title TEXT,
  permalink TEXT,
  posted_at REAL,
  views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER,
  metrics_source TEXT,
  metrics_at REAL,
  favourite INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0,
  features_json TEXT DEFAULT '{}',
  analyzed_at REAL,
  notes TEXT DEFAULT '',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS glossary (
  id INTEGER PRIMARY KEY,
  term TEXT NOT NULL,
  aliases_json TEXT DEFAULT '[]',
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER,
  path TEXT,
  params_json TEXT,
  probe_json TEXT,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS game_events (
  id INTEGER PRIMARY KEY,
  game TEXT NOT NULL,
  t_wall REAL NOT NULL,
  kind TEXT NOT NULL,
  data_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS gev_time ON game_events(t_wall);
"""

JSON_COLS = {"info_json", "track_roles_json", "options_json", "payload_json", "result_json", "evidence_json",
             "features_json", "hooks_json", "captions_json", "edit_json", "data_json", "aliases_json",
             "params_json", "probe_json"}
LIST_COLS = {"evidence_json", "hooks_json", "captions_json", "aliases_json"}

DEFAULT_GLOSSARY = [
    ("fortysevenfrags", ["forty seven frags", "47 frags", "fortyseven frags", "forty-seven frags"]),
    ("CS2", ["cs two", "c s 2"]),
    ("Valorant", ["valo rant"]),
]


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = {}
    for k in row.keys():
        v = row[k]
        if k in JSON_COLS:
            name = k[:-5]
            empty = [] if k in LIST_COLS else {}
            try:
                out[name] = json.loads(v) if v else empty
            except (TypeError, json.JSONDecodeError):
                out[name] = empty
        else:
            out[k] = v
    return out


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _init(self):
        c = self.conn()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise RuntimeError(f"database is from a newer ClipSift (schema {ver})")
        c.executescript(SCHEMA)
        if ver < 1:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if not c.execute("SELECT 1 FROM glossary LIMIT 1").fetchone():
                for term, aliases in DEFAULT_GLOSSARY:
                    c.execute("INSERT INTO glossary(term, aliases_json) VALUES (?,?)", (term, json.dumps(aliases)))

    def execute(self, sql, params=()):
        return self.conn().execute(sql, params)

    def one(self, sql, params=()) -> dict | None:
        return _row_to_dict(self.conn().execute(sql, params).fetchone())

    def all(self, sql, params=()) -> list[dict]:
        return [_row_to_dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def insert(self, table: str, **fields) -> int:
        fields = _encode(fields)
        cols = ",".join(fields)
        qs = ",".join("?" * len(fields))
        cur = self.conn().execute(f"INSERT INTO {table}({cols}) VALUES ({qs})", tuple(fields.values()))
        return cur.lastrowid

    def update(self, table: str, row_id: int, **fields) -> None:
        if not fields:
            return
        fields = _encode(fields)
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))

    # settings

    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for r in self.conn().execute("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                pass
        return out

    def set_settings(self, values: dict) -> None:
        c = self.conn()
        c.execute("BEGIN")
        try:
            for k, v in values.items():
                if k not in DEFAULT_SETTINGS:
                    raise KeyError(f"unknown setting: {k}")
                c.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, json.dumps(v)))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # glossary

    def glossary(self) -> list[dict]:
        return self.all("SELECT * FROM glossary ORDER BY term COLLATE NOCASE")

    def replace_glossary(self, entries: list[dict]) -> None:
        c = self.conn()
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM glossary")
            for e in entries:
                term = str(e.get("term", "")).strip()
                if not term:
                    continue
                aliases = [str(a).strip() for a in e.get("aliases", []) if str(a).strip()]
                c.execute("INSERT INTO glossary(term, aliases_json, note) VALUES (?,?,?)",
                          (term, json.dumps(aliases, ensure_ascii=False), str(e.get("note", ""))))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # jobs

    def add_job(self, kind: str, lane: str, source_id=None, candidate_id=None, payload=None) -> int:
        now = time.time()
        return self.insert("jobs", kind=kind, lane=lane, source_id=source_id, candidate_id=candidate_id,
                           payload_json=payload or {}, status="queued", created_at=now, updated_at=now)

    def claim_job(self, lane: str, pid: int) -> dict | None:
        c = self.conn()
        for _ in range(5):
            row = c.execute("SELECT id FROM jobs WHERE status='queued' AND lane=? ORDER BY id LIMIT 1", (lane,)).fetchone()
            if not row:
                return None
            now = time.time()
            cur = c.execute("UPDATE jobs SET status='running', worker_pid=?, started_at=COALESCE(started_at, ?), "
                            "updated_at=?, cancel_requested=0, error=NULL WHERE id=? AND status='queued'",
                            (pid, now, now, row["id"]))
            if cur.rowcount == 1:
                return self.job(row["id"])
        return None

    def job(self, job_id: int) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def active_job_for(self, kind: str, source_id=None, candidate_id=None) -> dict | None:
        """Queued or running job of this kind; filters only on the ids that are given."""
        q, args = "SELECT * FROM jobs WHERE kind=? AND status IN ('queued','running')", [kind]
        if source_id is not None:
            q += " AND source_id=?"
            args.append(source_id)
        if candidate_id is not None:
            q += " AND candidate_id=?"
            args.append(candidate_id)
        return self.one(q + " ORDER BY id DESC LIMIT 1", tuple(args))

    def requeue_interrupted(self, pid: int | None = None) -> list[int]:
        """Jobs left 'running' by a dead worker go back to the queue."""
        rows = self.all("SELECT id, crash_count FROM jobs WHERE status='running'")
        ids = []
        for r in rows:
            crashes = (r["crash_count"] or 0) + 1
            if crashes >= 3:
                self.update("jobs", r["id"], status="failed", crash_count=crashes, finished_at=time.time(),
                            error="The worker stopped 3 times while running this job. See the logs.")
            else:
                self.update("jobs", r["id"], status="queued", crash_count=crashes, updated_at=time.time(),
                            message="Resuming after restart")
                ids.append(r["id"])
        return ids


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in JSON_COLS and not isinstance(v, (str, type(None))):
            v = json.dumps(v, ensure_ascii=False, default=str)
        out[k] = v
    return out

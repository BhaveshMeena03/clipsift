"""Launcher: start the local server, keep the worker running, open the app window."""
import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .config import IS_WINDOWS, Paths
from .log import setup_logging

log = logging.getLogger("clipsift.app")
DEFAULT_PORT = 8765
STALL_S = 15 * 60  # a running job with no progress for this long means a native hang


class WorkerSupervisor:
    """Keeps one worker process alive. Restarts it after a crash, with backoff."""

    def __init__(self, paths: Paths, state: dict):
        self.paths = paths
        self.state = state
        self.proc: subprocess.Popen | None = None
        self.stopping = threading.Event()
        self.restarts: list[float] = []

    def _spawn(self):
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        self.proc = subprocess.Popen([sys.executable, "-m", "clipsift.worker", "--home", str(self.paths.home),
                                      "--parent-pid", str(os.getpid())],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.state["worker"] = "running"
        log.info("worker started (pid %s)", self.proc.pid)

    def stalled_job(self) -> dict | None:
        from .db import DB
        if not hasattr(self, "_db"):
            self._db = DB(self.paths.db)
        row = self._db.one("SELECT id, kind, updated_at FROM jobs WHERE status='running' AND worker_pid=? "
                           "ORDER BY updated_at LIMIT 1", (self.proc.pid,))
        if row and row["updated_at"] and time.time() - row["updated_at"] > STALL_S:
            return row
        return None

    def run(self):
        self._spawn()
        last_watch = time.time()
        while not self.stopping.is_set():
            if self.proc.poll() is None and time.time() - last_watch > 30:
                last_watch = time.time()
                try:
                    stuck = self.stalled_job()
                except Exception as e:
                    log.warning("watchdog check failed: %s", e)
                    stuck = None
                if stuck:
                    log.error("job %s (%s) made no progress for %d minutes; restarting the worker",
                              stuck["id"], stuck["kind"], STALL_S // 60)
                    self.proc.kill()
                    self.proc.wait(timeout=30)
            code = self.proc.poll()
            if code is not None and not self.stopping.is_set():
                if code == 2:
                    # an old worker still holds the lock; it notices its app is gone within seconds
                    self.lock_waits = getattr(self, "lock_waits", 0) + 1
                    if self.lock_waits > 10:
                        self.state["worker"] = "another copy of ClipSift is already running"
                        log.error("worker exited: another instance holds the lock")
                        return
                    self.state["worker"] = "waiting for the previous worker to stop"
                    time.sleep(3)
                    self._spawn()
                    continue
                now = time.time()
                self.restarts = [t for t in self.restarts if now - t < 300] + [now]
                if len(self.restarts) > 5:
                    self.state["worker"] = "stopped after repeated crashes; see logs"
                    log.error("worker crashed 5 times in 5 minutes; not restarting")
                    return
                self.state["worker"] = "restarting"
                log.warning("worker exited with %s, restarting", code)
                time.sleep(min(30, 2 ** len(self.restarts)))
                self._spawn()
            self.stopping.wait(1.0)

    def stop(self, timeout: float = 30.0):
        self.stopping.set()
        if self.proc and self.proc.poll() is None:
            # per-pid name, so a slow old app can never stop a newer app's worker
            (self.paths.home / f"worker.stop.{self.proc.pid}").touch()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("worker did not stop in time, killing it")
                self.proc.kill()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if not IS_WINDOWS:
            # same as uvicorn; a closed connection in TIME_WAIT must not count as busy
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def clipsift_at(port: int) -> bool:
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode()).get("app") == "clipsift"
    except Exception:
        return False


def pick_port(preferred: int) -> tuple[int, bool]:
    """(port, already_running). Waits a moment for a restarting copy to let go of the port."""
    for _ in range(12):
        if port_free(preferred):
            return preferred, False
        if clipsift_at(preferred):
            return preferred, True
        time.sleep(0.5)
    for port in range(preferred + 1, preferred + 20):
        if port_free(port):
            return port, False
    raise RuntimeError("No free local port found")


def open_window(url: str) -> None:
    """Edge app mode gives a plain window on Windows; otherwise the default browser."""
    if IS_WINDOWS:
        candidates = [shutil.which("msedge"),
                      os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                      os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
        for c in candidates:
            if c and Path(c).is_file():
                subprocess.Popen([c, f"--app={url}", "--window-size=1440,920"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                return
    webbrowser.open(url)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clipsift")
    ap.add_argument("--home", default=None, help="data folder (default %%LOCALAPPDATA%%\\ClipSift)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--no-worker", action="store_true")
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else None).ensure()
    setup_logging(paths.logs, "server")
    port, running = pick_port(args.port)
    if running:
        print(f"ClipSift is already running at http://127.0.0.1:{port}/", flush=True)
        if not args.no_window:
            open_window(f"http://127.0.0.1:{port}/")
        return 0
    if port != args.port:
        log.warning("port %s is used by another program, using %s; CS2/Dota event feeds will not reach "
                    "ClipSift until that port is free", args.port, port)
    state = {"port": port, "worker": "starting"}
    import uvicorn

    from .server.app import create_app
    sup = None if args.no_worker else WorkerSupervisor(paths, state)
    app = create_app(paths, state, on_shutdown=sup.stop if sup else None)
    if sup:
        threading.Thread(target=sup.run, name="worker-supervisor", daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if not args.no_window:
        threading.Timer(1.2, open_window, args=(url,)).start()
    print(f"ClipSift is running at {url}  (close this window to quit)", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    finally:
        if sup:
            sup.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

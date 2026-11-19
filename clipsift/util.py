import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path


class Cancelled(Exception):
    pass


def fmt_ts(seconds: float, ms: bool = False) -> str:
    if seconds is None:
        return ""
    neg = seconds < 0
    seconds = abs(float(seconds))
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    if ms:
        out += f".{int(round((seconds - whole) * 1000)) % 1000:03d}"
    return ("-" if neg else "") + out


def parse_ts(text: str) -> float:
    """'1:02:03.5' / '62:03' / '123.4' -> seconds."""
    text = str(text).strip()
    if not text:
        raise ValueError("empty timestamp")
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
        raise ValueError(f"bad timestamp: {text!r}")
    parts = text.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace(src, dst, tries: int = 20) -> None:
    # Windows refuses the rename while another process has dst open, so retry briefly.
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05 * (i + 1))


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1, default=_json_default))


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # half-written files from a crash are treated as missing
        return default


def stable_hash(obj) -> str:
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def file_fingerprint(path: Path) -> str:
    """Size + mtime + head/tail bytes. Cheap enough for 10 GB files."""
    path = Path(path)
    st = path.stat()
    h = hashlib.sha1()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if st.st_size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()[:16]


def safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if name.upper().split(".")[0] in reserved:
        name = "_" + name
    return (name[:max_len].rstrip(" .") or "clip")


def unique_path(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def dir_size(path: Path) -> int:
    total = 0
    if not Path(path).exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def find_on_path(name: str) -> str | None:
    """Like shutil.which, but never the current folder. On Windows, Python 3.11's which() looks in the
    current folder first, so an ffmpeg.exe dropped there would run instead of the real one."""
    exe = name + (".exe" if os.name == "nt" and not name.lower().endswith(".exe") else "")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d):
            continue
        cand = os.path.join(d, exe)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def interpreter_pid(proc) -> int:
    """PID of the Python that really runs a child started with sys.executable. On Windows,
    .venv\\Scripts\\python.exe is a small launcher that starts the real python.exe as its own child,
    so proc.pid is the launcher, not the process that names stop files, holds locks and claims jobs."""
    import psutil
    try:
        kids = [c for c in psutil.Process(proc.pid).children() if c.name().lower().startswith("python")]
    except psutil.Error:
        return proc.pid
    return kids[0].pid if len(kids) == 1 else proc.pid


def kill_tree(proc, timeout: float = 10.0) -> None:
    """Hard kill a child and everything it started (the real Python behind a venv launcher, ffmpeg),
    and wait until they are gone so nothing still holds the worker lock."""
    import psutil
    try:
        procs = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        procs = []
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    proc.kill()
    psutil.wait_procs(procs, timeout=timeout)
    proc.wait(timeout=timeout)

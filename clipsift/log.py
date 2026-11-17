import logging
import logging.handlers
import re
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(access_token=)[^&\s\"']+", re.I), r"\1[redacted]"),
    (re.compile(r"(\"?(?:token|auth|password|secret|api_key)\"?\s*[:=]\s*\"?)[^\s\",&]+", re.I), r"\1[redacted]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I), r"\1[redacted]"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}\b"), "[redacted]"),  # Meta Graph tokens
]

_extra_secrets: set[str] = set()


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for s in _extra_secrets:
        text = text.replace(s, "[redacted]")
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging(log_dir: Path, name: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_clipsift", False):
            root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh._clipsift = True
    sh = logging.StreamHandler()
    sh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    sh._clipsift = True
    root.addHandler(fh)
    root.addHandler(sh)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "urllib3", "huggingface_hub", "faster_whisper", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def tail(path: Path, lines: int = 300) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            data = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return [redact(x) for x in data.splitlines()[-lines:]]

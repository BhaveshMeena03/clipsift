"""The UI is served on 127.0.0.1 only. A Host check keeps other websites (and DNS rebinding) out, and the
API needs a cookie that is only handed out for a one-time launch code. Codes are signed with the install's
secret in server.token, so only someone who can read that file (the user who installed it) can open a
working window; other programs and other Windows accounts on the PC get the page but no API access."""
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE = "cs_token"
HEADER = "x-clipsift"
LAUNCH_TTL_S = 120
GSI_MAX_BYTES = 64 * 1024  # CS2/Dota send a few KB per update


def load_token(home: Path) -> str:
    """Random once per install, so an open window survives an app restart."""
    f = Path(home) / "server.token"
    try:
        tok = f.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    f.write_text(tok, encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return tok


def sign(token: str, message: str) -> str:
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    """Constant-time compare that also copes with non-ASCII input (hmac rejects that for str)."""
    return hmac.compare_digest(str(a).encode("utf-8", "replace"), str(b).encode("utf-8", "replace"))


def launch_code(token: str, now: float | None = None) -> str:
    nonce = f"{int(now if now is not None else time.time())}.{secrets.token_hex(8)}"
    return f"{nonce}.{sign(token, 'launch:' + nonce)}"


class LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app, port_ref: dict, token: str | None = None):
        super().__init__(app)
        self.port_ref = port_ref
        self.token = token or secrets.token_urlsafe(32)
        self.used_codes: dict[str, float] = {}

    def allowed_host(self, host: str) -> bool:
        port = self.port_ref.get("port")
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def use_launch_code(self, code: str) -> bool:
        now = time.time()
        try:
            ts, rnd, mac = code.split(".")
            age = now - int(ts)
        except ValueError:
            return False
        if not -5 <= age <= LAUNCH_TTL_S or not same(mac, sign(self.token, f"launch:{ts}.{rnd}")):
            return False
        self.used_codes = {c: t for c, t in self.used_codes.items() if now - t <= LAUNCH_TTL_S + 10}
        if code in self.used_codes:
            return False
        self.used_codes[code] = now
        return True

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if not self.allowed_host(host):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        path = request.url.path
        if path.startswith("/gsi/"):
            # game state feeds authenticate with their own token in the payload; refuse big bodies unread
            try:
                size = int(request.headers.get("content-length", ""))
            except ValueError:
                return JSONResponse({"error": "length required"}, status_code=411)
            if size > GSI_MAX_BYTES:
                return JSONResponse({"error": "too large"}, status_code=413)
            return await call_next(request)
        if path == "/api/ping":
            return await call_next(request)
        if path.startswith("/api/") or path.startswith("/media/"):
            if not same(request.cookies.get(COOKIE, ""), self.token):
                return JSONResponse({"error": "not authorized: open ClipSift from its desktop shortcut"},
                                    status_code=401)
            if request.method not in ("GET", "HEAD") and request.headers.get(HEADER) != "1":
                return JSONResponse({"error": "missing header"}, status_code=403)
        response = await call_next(request)
        if path == "/" and self.use_launch_code(request.query_params.get("k", "")):
            response.set_cookie(COOKIE, self.token, httponly=True, samesite="strict")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

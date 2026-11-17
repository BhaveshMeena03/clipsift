"""The UI is served on 127.0.0.1 only. A per-launch cookie plus a Host check keeps other
websites (and DNS rebinding) from driving the local API."""
import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE = "cs_token"
HEADER = "x-clipsift"


class LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app, port_ref: dict, token: str | None = None):
        super().__init__(app)
        self.port_ref = port_ref
        self.token = token or secrets.token_urlsafe(32)

    def allowed_host(self, host: str) -> bool:
        port = self.port_ref.get("port")
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if not self.allowed_host(host):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        path = request.url.path
        # game state feeds authenticate with their own token in the payload
        if path.startswith("/gsi/") or path == "/api/ping":
            return await call_next(request)
        if path.startswith("/api/") or path.startswith("/media/"):
            tok = request.cookies.get(COOKIE, "")
            if not hmac.compare_digest(tok, self.token):
                return JSONResponse({"error": "not authorized, reload the app"}, status_code=401)
            if request.method not in ("GET", "HEAD") and request.headers.get(HEADER) != "1":
                return JSONResponse({"error": "missing header"}, status_code=403)
        response = await call_next(request)
        if path == "/" or path.endswith(".html"):
            response.set_cookie(COOKIE, self.token, httponly=True, samesite="strict")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

"""Optional Instagram metrics.

Supported, permitted routes:
  1. CSV export from Meta Business Suite / Professional Dashboard (Insights > Content > Export).
  2. Instagram Graph API with a token you create for your own professional account.
Best effort only:
  3. Public post metadata through yt-dlp with no login. Often blocked; then enter numbers by hand.
ClipSift never logs in to Instagram or works around its login wall."""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"


class InstagramError(RuntimeError):
    pass


def _get(url: str, params: dict, timeout: float = 30) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "ClipSift"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise InstagramError(f"Instagram API error {e.code}: {msg or e.reason}") from None
    except urllib.error.URLError as e:
        raise InstagramError(f"Could not reach the Instagram API: {e.reason}") from None


def graph_fetch(user_id: str, token: str, limit: int = 200, get=_get) -> list[dict]:
    """Media plus counts for your own professional account."""
    if not user_id or not token:
        raise InstagramError("Set your Instagram professional account id and access token in Settings first.")
    rows = []
    url = f"{GRAPH}/{user_id}/media"
    params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count",
              "limit": 50, "access_token": token}
    while url and len(rows) < limit:
        data = get(url, params)
        for m in data.get("data", []):
            if m.get("media_type") != "VIDEO" and m.get("media_product_type") != "REELS":
                continue
            row = {"permalink": m.get("permalink"), "title": (m.get("caption") or "")[:200] or None,
                   "posted_at": _ts(m.get("timestamp")), "likes": m.get("like_count"),
                   "comments": m.get("comments_count"), "views": None, "shares": None, "saves": None}
            try:
                ins = get(f"{GRAPH}/{m['id']}/insights", {"metric": "views,shares,saved", "access_token": token})
                for item in ins.get("data", []):
                    val = (item.get("values") or [{}])[0].get("value")
                    key = {"views": "views", "shares": "shares", "saved": "saves"}.get(item.get("name"))
                    if key and isinstance(val, int):
                        row[key] = val
            except InstagramError as e:
                log.info("insights not available for %s: %s", m.get("id"), e)
            rows.append(row)
        nxt = (data.get("paging") or {}).get("next")
        url, params = (nxt, {}) if nxt else (None, None)
    return rows


def _ts(v):
    from .references import parse_date
    return parse_date(v)


def public_post_meta(url: str) -> dict:
    """No login, no cookies. Fails cleanly when Instagram wants a login."""
    import yt_dlp
    if "instagram.com/" not in url:
        raise InstagramError("Not an Instagram link.")
    params = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 20}
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "login" in msg or "rate" in msg or "cookies" in msg:
            raise InstagramError("Instagram wants a login to show this post. Enter the numbers by hand instead.") from None
        raise InstagramError(f"Could not read this post: {str(e).splitlines()[-1][:200]}") from None
    return {"permalink": url, "title": (info.get("description") or info.get("title") or "")[:200] or None,
            "posted_at": info.get("timestamp"), "views": info.get("view_count"), "likes": info.get("like_count"),
            "comments": info.get("comment_count"), "shares": None, "saves": None}

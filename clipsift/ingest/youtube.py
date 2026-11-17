"""YouTube VOD download with yt-dlp. Public videos only: no cookies, no login, no workarounds
for private, members-only, age-gated or region-locked videos."""
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import IS_WINDOWS
from ..media.disk import ensure_space
from ..util import Cancelled, fmt_ts, human_bytes

log = logging.getLogger(__name__)

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
# best = highest resolution and frame rate on offer (often 1440p/4K VP9 or AV1 for streams);
# H.264 only wins a tie, since it decodes fastest
QUALITY = {
    "best": "bv*[dynamic_range=SDR]+ba/bv*+ba/b",  # HDR would look washed out in an SDR export
    "1080": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
}


class YTError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise YTError("invalid", "Paste a YouTube link first.")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host not in HOSTS:
        raise YTError("unsupported", "Only YouTube links are supported here. For other files, download them yourself and add them as local files.")
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif u.path.startswith("/watch"):
        vid = (parse_qs(u.query).get("v") or [""])[0]
    elif u.path.startswith(("/live/", "/shorts/", "/embed/")):
        vid = u.path.split("/")[2] if len(u.path.split("/")) > 2 else ""
    else:
        raise YTError("unsupported", "That looks like a channel or playlist link. Paste the link to one video or stream.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
        raise YTError("invalid", "Could not find a video id in that link.")
    return f"https://www.youtube.com/watch?v={vid}"


_ERRORS = [
    (r"private video|this video is private", "private", "This video is private. Only public or unlisted videos can be downloaded."),
    (r"members[- ]only|join this channel|available to this channel's members", "members", "This video is for channel members only."),
    (r"sign in to confirm your age|age[- ]restricted|inappropriate for some users", "age", "This video is age-restricted and needs a signed-in account. ClipSift does not sign in to YouTube. Download it yourself and add the file."),
    (r"available in your country|geo.?restrict|blocked it in your country|not available in your region", "geo", "This video is not available in your region."),
    (r"premieres in|will begin in|this live event will begin|is upcoming", "upcoming", "This stream has not started yet."),
    (r"sign in to confirm you.?re not a bot|confirm you are not a robot", "bot_check", "YouTube is asking for a bot check right now. Try again later. ClipSift does not bypass it."),
    (r"video (is )?unavailable|no longer available|has been removed|account .* terminated|does not exist", "unavailable", "This video is unavailable (removed or never existed)."),
    (r"copyright", "unavailable", "This video was taken down for copyright."),
    (r"http error 429|too many requests", "rate_limited", "YouTube is rate-limiting downloads right now. Try again later."),
    (r"unable to download webpage|timed out|connection|network|getaddrinfo|ssl", "network", "Could not reach YouTube. Check the internet connection."),
    (r"no supported javascript runtime|js runtime|ejs", "setup", "yt-dlp needs Deno for YouTube. Run setup again or set the Deno path in Settings."),
]


def classify(message: str) -> YTError:
    m = message.lower()
    for pat, kind, text in _ERRORS:
        if re.search(pat, m):
            return YTError(kind, text)
    return YTError("other", "YouTube download failed: " + message.strip().splitlines()[-1][:300])


def deno_path(paths, settings) -> str | None:
    if settings.get("deno_path") and Path(settings["deno_path"]).is_file():
        return settings["deno_path"]
    exe = "deno.exe" if IS_WINDOWS else "deno"
    for cand in (paths.tools / "deno" / exe, paths.tools / exe):
        if cand.is_file():
            return str(cand)
    return shutil.which("deno")


def base_params(paths, settings) -> dict:
    p = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30, "retries": 10,
         "fragment_retries": 20, "continuedl": True, "noprogress": True, "logger": _Quiet()}
    d = deno_path(paths, settings)
    if d:
        p["js_runtimes"] = {"deno": {"path": d}}
    from ..media.ffmpeg import ffmpeg_bin
    try:
        p["ffmpeg_location"] = str(Path(ffmpeg_bin()).parent)
    except Exception:
        pass
    return p


class _Quiet:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.info("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp: %s", msg)


def _check_status(info: dict) -> None:
    status = info.get("live_status")
    if status == "is_live" or info.get("is_live"):
        raise YTError("live", "This stream is still live. Add it again after it ends and YouTube has processed it.")
    if status == "is_upcoming":
        raise YTError("upcoming", "This stream has not started yet.")
    if status == "post_live":
        raise YTError("post_live", "The stream just ended and YouTube is still processing the recording. "
                                   "Downloading now may only get part of it. Try again in a while.")
    avail = info.get("availability")
    if avail in ("private", "premium_only", "subscriber_only", "needs_auth"):
        raise classify({"private": "private video", "premium_only": "members-only",
                        "subscriber_only": "members-only", "needs_auth": "sign in to confirm your age"}[avail])


def estimate_size(info: dict, quality: str) -> int:
    fmts = info.get("requested_formats") or ([info] if info.get("format_id") else [])
    total = 0
    for f in fmts:
        total += int(f.get("filesize") or f.get("filesize_approx") or 0)
    if not total:
        dur = float(info.get("duration") or 0)
        mbps = {"best": 18.0, "1080": 9.0}.get(quality, 4.5)
        total = int(dur * mbps * 1e6 / 8)
    return total


SORT = ["res", "fps", "vcodec:h264", "acodec:aac"]


def check(url: str, paths, settings, quality: str = "best") -> dict:
    import yt_dlp
    url = normalize_url(url)
    params = base_params(paths, settings) | {"skip_download": True, "format": QUALITY.get(quality, QUALITY["best"]),
                                             "format_sort": SORT}
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise classify(str(e)) from None
    if info.get("_type") == "playlist":
        raise YTError("unsupported", "That is a playlist. Paste one video link.")
    _check_status(info)
    size = estimate_size(info, quality)
    return {"url": url, "id": info.get("id"), "title": info.get("title"), "channel": info.get("channel"),
            "duration": info.get("duration"), "duration_text": fmt_ts(info.get("duration") or 0),
            "live_status": info.get("live_status"), "was_live": info.get("live_status") == "was_live",
            "started_at": info.get("release_timestamp") or info.get("timestamp"),
            "height": info.get("height"), "fps": info.get("fps"), "vcodec": info.get("vcodec"), "est_bytes": size, "est_text": human_bytes(size), "quality": quality,
            "thumbnail": info.get("thumbnail")}


def run_download(ctx) -> dict:
    import yt_dlp
    db, paths = ctx.db, ctx.paths
    src = db.one("SELECT * FROM sources WHERE id=?", (ctx.job["source_id"],))
    payload = ctx.job.get("payload") or {}
    quality = payload.get("quality") or ctx.settings.get("download_quality") or "best"
    ctx.progress("download", 0.0, "Checking the video")
    meta = check(src["url"], paths, ctx.settings, quality)
    out_dir = paths.downloads / meta["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    # parts + merged copy both exist for a moment
    ensure_space(out_dir, int(meta["est_bytes"] * 2.1), f"downloading {meta['title']}")
    db.update("sources", src["id"], title=meta["title"], video_id=meta["id"], status="downloading",
              recorded_at=meta.get("started_at"), duration=meta.get("duration"), updated_at=time.time())
    state = {"last": 0.0}

    def hook(d):
        if ctx.cancelled():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = done / total if total else 0.0
            now = time.time()
            if now - state["last"] > 0.5:
                state["last"] = now
                speed = d.get("speed") or 0
                eta = d.get("eta")
                part = d.get("info_dict", {}).get("format_id", "")
                msg = f"Downloading {human_bytes(done)} of {human_bytes(total) if total else '?'}"
                if speed:
                    msg += f" at {human_bytes(speed)}/s"
                if eta:
                    msg += f", about {fmt_ts(eta)} left (estimate)"
                ctx.progress("download", min(0.99, frac), msg + (f" [{part}]" if part else ""))

    params = base_params(paths, ctx.settings) | {
        "format": QUALITY.get(quality, QUALITY["best"]), "format_sort": SORT, "merge_output_format": "mp4/mkv",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"), "progress_hooks": [hook],
        "postprocessor_hooks": [lambda d: ctx.progress("download", 0.995, "Joining audio and video")],
    }
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(meta["url"], download=True)
            final = Path(info.get("filepath") or (info.get("requested_downloads") or [{}])[0].get("filepath")
                         or ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadCancelled:
        ctx.check()
        raise Cancelled() from None
    except yt_dlp.utils.DownloadError as e:
        ctx.check()
        raise classify(str(e)) from None
    if not final.is_file():
        cands = sorted(out_dir.glob(f"{meta['id']}.*"), key=lambda p: p.stat().st_size, reverse=True)
        cands = [c for c in cands if c.suffix in (".mp4", ".mkv", ".webm") and ".part" not in c.name]
        if not cands:
            raise RuntimeError("Download finished but the file was not found.")
        final = cands[0]
    db.update("sources", src["id"], path=str(final), status="new", updated_at=time.time())
    if payload.get("auto_process", True):
        if not db.active_job_for("process", source_id=src["id"]):
            db.add_job("process", "heavy", source_id=src["id"])
    return {"path": str(final), "title": meta["title"], "message": f"Downloaded {final.name}"}

"""YouTube download job with yt-dlp replaced by a fake, so no network or file download happens."""
import time

import pytest

from clipsift.ingest import youtube
from clipsift.media.disk import DiskSpaceError
from clipsift.pipeline.context import JobContext
from clipsift.util import Cancelled
from tests.conftest import make_home


class FakeYDL:
    calls: list = []
    cancel_after = None

    def __init__(self, params):
        self.params = params

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append(("extract", download, self.params.get("format"), "cookiefile" in self.params))
        info = {"id": "abcdefghijk", "title": "Stream night", "duration": 3 * 3600, "live_status": "was_live",
                "availability": "public", "release_timestamp": 1_790_000_000, "height": 1440, "fps": 60, "vcodec": "vp9",
                "requested_formats": [{"filesize": 6_000_000_000}, {"filesize": 200_000_000}]}
        if download:
            out = self.params["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "mp4")
            for i, hook in enumerate(self.params["progress_hooks"] * 5):
                hook({"status": "downloading", "downloaded_bytes": (i + 1) * 10**9, "total_bytes": 6.2 * 10**9,
                      "speed": 5 * 10**7, "eta": 100, "info_dict": {"format_id": "308"}})
                if FakeYDL.cancel_after is not None and i >= FakeYDL.cancel_after:
                    break
            with open(out, "wb") as f:
                f.write(b"\0" * 1000)
            info["filepath"] = out
            info["requested_downloads"] = [{"filepath": out}]
        return info

    def prepare_filename(self, info):
        return self.params["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "mp4")


@pytest.fixture
def fake_ytdlp(monkeypatch):
    import yt_dlp
    FakeYDL.calls = []
    FakeYDL.cancel_after = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def _ctx(paths, db, jid):
    return JobContext(db, paths, db.job(jid), db.settings(), stages=[("download", 1)])


def test_download_job(tmp_path, fake_ytdlp, monkeypatch):
    from clipsift.media import disk
    monkeypatch.setattr(disk, "free_bytes", lambda p: 500 * 1024**3)
    paths, db = make_home(tmp_path)
    sid = db.insert("sources", kind="youtube", url="https://www.youtube.com/watch?v=abcdefghijk", title="x", created_at=time.time())
    jid = db.add_job("download", "download", source_id=sid, payload={})
    db.claim_job("download", 1)
    ctx = _ctx(paths, db, jid)
    seen = []
    real = ctx.progress
    ctx.progress = lambda stage, frac, message=None, force=False: (seen.append((frac, message)), real(stage, frac, message, True))
    res = youtube.run_download(ctx)
    src = db.one("SELECT * FROM sources WHERE id=?", (sid,))
    assert src["path"].endswith("abcdefghijk.mp4") and src["title"] == "Stream night"
    assert src["recorded_at"] == 1_790_000_000      # used to line up game events
    assert "Downloaded" in res["message"]
    assert db.active_job_for("process", source_id=sid)  # processing queued after download
    assert all(not cookies for _k, _d, _f, cookies in fake_ytdlp.calls)  # never sends cookies
    assert fake_ytdlp.calls[-1][2] == youtube.QUALITY["best"]
    msgs = [m for _f, m in seen if m and m.startswith("Downloading")]
    assert msgs and "(estimate)" in msgs[-1] and "[308]" in msgs[-1]
    fracs = [f for f, m in seen if m and m.startswith("Downloading")]
    assert fracs == sorted(fracs) and max(fracs) < 1.0


def test_download_refuses_without_space(tmp_path, fake_ytdlp, monkeypatch):
    from clipsift.media import disk
    monkeypatch.setattr(disk, "free_bytes", lambda p: 5 * 1024**3)  # 5 GB free, ~13 GB needed
    paths, db = make_home(tmp_path)
    sid = db.insert("sources", kind="youtube", url="https://www.youtube.com/watch?v=abcdefghijk", title="x", created_at=time.time())
    jid = db.add_job("download", "download", source_id=sid, payload={})
    with pytest.raises(DiskSpaceError):
        youtube.run_download(_ctx(paths, db, jid))
    assert not any(c[1] for c in fake_ytdlp.calls)  # never started downloading


def test_download_cancel(tmp_path, fake_ytdlp, monkeypatch):
    from clipsift.media import disk
    monkeypatch.setattr(disk, "free_bytes", lambda p: 500 * 1024**3)
    paths, db = make_home(tmp_path)
    sid = db.insert("sources", kind="youtube", url="https://www.youtube.com/watch?v=abcdefghijk", title="x", created_at=time.time())
    jid = db.add_job("download", "download", source_id=sid, payload={})
    db.update("jobs", jid, cancel_requested=1)
    import yt_dlp

    class CancellingYDL(FakeYDL):
        def extract_info(self, url, download=False):
            if download:
                self.params["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
            return super().extract_info(url, download)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", CancellingYDL)
    with pytest.raises(Cancelled):
        youtube.run_download(_ctx(paths, db, jid))
    assert not db.active_job_for("process", source_id=sid)


def test_check_refuses_live(monkeypatch, tmp_path):
    import yt_dlp

    class LiveYDL(FakeYDL):
        def extract_info(self, url, download=False):
            return {"id": "abcdefghijk", "live_status": "is_live", "title": "LIVE"}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", LiveYDL)
    paths, _db = make_home(tmp_path)
    with pytest.raises(youtube.YTError) as e:
        youtube.check("https://youtu.be/abcdefghijk", paths, {})
    assert e.value.kind == "live"

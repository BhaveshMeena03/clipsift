import time

import pytest
from fastapi.testclient import TestClient

from clipsift.config import Paths
from clipsift.ingest.youtube import YTError, classify, normalize_url
from clipsift.prefs.model import performance, suggest
from clipsift.prefs.references import parse_csv, parse_date, parse_number, upsert_metrics
from clipsift.server.app import create_app
from tests.conftest import needs_media

H = {"X-ClipSift": "1"}


@pytest.fixture
def client(tmp_path):
    paths = Paths(tmp_path / "home")
    app = create_app(paths, {"port": 8765}, token="t" * 40)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.get("/")  # sets the cookie
    c.paths = paths
    c.db = app.state.db
    return c


# ---------- security ----------

def test_api_needs_cookie(tmp_path):
    app = create_app(Paths(tmp_path / "h"), {"port": 8765}, token="x" * 40)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/state").status_code == 401
    c.get("/")
    assert c.get("/api/state").status_code == 200


def test_foreign_host_rejected(client):
    r = client.get("/api/state", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403


def test_writes_need_custom_header(client):
    assert client.put("/api/settings", json={"max_candidates": 5}).status_code == 403
    assert client.put("/api/settings", json={"max_candidates": 5}, headers=H).status_code == 200


def test_cookie_is_strict_and_httponly(tmp_path):
    app = create_app(Paths(tmp_path / "h"), {"port": 8765}, token="y" * 40)
    r = TestClient(app, base_url="http://127.0.0.1:8765").get("/")
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc


def test_ping_is_open(tmp_path):
    app = create_app(Paths(tmp_path / "h"), {"port": 8765}, token="z" * 40)
    assert TestClient(app, base_url="http://127.0.0.1:8765").get("/api/ping").json()["app"] == "clipsift"


def test_token_persists_across_launches(tmp_path):
    p = Paths(tmp_path / "h")
    a = TestClient(create_app(p, {"port": 8765}), base_url="http://127.0.0.1:8765")
    a.get("/")
    b = TestClient(create_app(p, {"port": 8765}), base_url="http://127.0.0.1:8765")
    b.cookies = a.cookies
    assert b.get("/api/state").status_code == 200


def test_secret_settings_are_masked(client):
    client.put("/api/settings", json={"instagram_graph_token": "EAAsecretsecretsecretsecret123"}, headers=H)
    assert client.get("/api/settings").json()["instagram_graph_token"] == "set"
    client.put("/api/settings", json={"instagram_graph_token": "set"}, headers=H)  # unchanged
    assert client.db.settings()["instagram_graph_token"] == "EAAsecretsecretsecretsecret123"


# ---------- validation ----------

def test_unknown_setting_rejected(client):
    assert client.put("/api/settings", json={"bogus": 1}, headers=H).status_code == 400


def test_layout_validation(client):
    bad = {"name": "x", "data": {"facecam": [0.9, 0.9, 0.5, 0.5]}}
    assert client.post("/api/layouts", json=bad, headers=H).status_code == 400
    ok = client.post("/api/layouts", json={"name": "mine", "data": {"facecam": [0, 0, 0.25, 0.25], "facecam_h": 0.9}},
                     headers=H)
    assert ok.status_code == 200 and ok.json()["data"]["facecam_h"] == 0.5
    assert client.post("/api/layouts", json={"name": "mine", "data": {}}, headers=H).status_code == 409


def test_add_missing_and_non_video_files(client, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    r = client.post("/api/sources/local", json={"paths": [str(tmp_path / "nope.mp4"), str(txt)]}, headers=H).json()
    assert r["added"] == [] and len(r["errors"]) == 2


@needs_media
def test_add_local_dedupes_and_reads_obs_time(client, tmp_path):
    from tests.conftest import ffmpeg
    f = tmp_path / "2026-11-17 20-15-03.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast", str(f))
    a = client.post("/api/sources/local", json={"paths": [str(f)]}, headers=H).json()
    b = client.post("/api/sources/local", json={"paths": [str(f)]}, headers=H).json()
    assert a["added"] == b["added"] and len(a["added"]) == 1
    src = client.get(f"/api/sources/{a['added'][0]}").json()
    assert time.localtime(src["recorded_at"]).tm_hour == 20
    assert client.get(f"/api/sources/{src['id']}/frame?t=1").headers["content-type"] == "image/jpeg"


def _cand(client, **kw):
    sid = client.db.insert("sources", kind="local", path="/x.mp4", title="x", duration=600.0, created_at=time.time())
    base = dict(source_id=sid, start=100.0, end=130.0, category="funny", score=0.5, rank_score=80.0,
                evidence_json=[{"kind": "tag_laugh", "tier": "model", "label": "laughter"}], captions_json={"words": [], "window": [80, 150]},
                edit_json={}, created_at=time.time())
    base.update(kw)
    return client.db.insert("candidates", **base)


def test_candidate_edit_rules(client):
    cid = _cand(client)
    url = f"/api/candidates/{cid}"
    assert client.patch(url, json={"edit": {"start": 129.5}}, headers=H).status_code == 400   # < 1 s
    assert client.patch(url, json={"edit": {"bogus": 1}}, headers=H).status_code == 400
    assert client.patch(url, json={"edit": {"hook": "x" * 200}}, headers=H).status_code == 400
    r = client.patch(url, json={"edit": {"start": 95.0, "end": 9999}}, headers=H)
    assert r.status_code == 200 and r.json()["bounds"] == [95.0, 600.0]
    assert client.patch(url, json={"status": "exported"}, headers=H).status_code == 400


def test_decisions_are_logged_for_tuning(client):
    cid = _cand(client)
    client.patch(f"/api/candidates/{cid}", json={"status": "accepted"}, headers=H)
    client.patch(f"/api/candidates/{cid}", json={"status": "rejected"}, headers=H)
    rows = client.db.all("SELECT * FROM decisions ORDER BY id")
    assert [r["decision"] for r in rows] == ["accepted", "rejected"]
    assert rows[0]["features"]["kinds"] == ["tag_laugh"]


def test_export_queues_one_job_per_clip(client):
    cid = _cand(client)
    a = client.post("/api/export", json={"candidate_ids": [cid]}, headers=H).json()["job_ids"]
    b = client.post("/api/export", json={"candidate_ids": [cid]}, headers=H).json()["job_ids"]
    assert a == b  # no duplicate export while one is queued
    assert client.post("/api/export", json={"candidate_ids": []}, headers=H).status_code == 400


def test_cancel_queued_and_resume(client):
    jid = client.db.add_job("process", "heavy", source_id=1)
    assert client.post(f"/api/jobs/{jid}/cancel", headers=H).json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{jid}/resume", headers=H).json()["status"] == "queued"
    assert client.post(f"/api/jobs/{jid}/resume", headers=H).status_code == 400


def test_delete_source_never_touches_local_file(client, tmp_path):
    f = tmp_path / "keep.mp4"
    f.write_bytes(b"data")
    sid = client.db.insert("sources", kind="local", path=str(f), title="k", created_at=time.time())
    client.delete(f"/api/sources/{sid}?delete_download=true", headers=H)
    assert f.read_bytes() == b"data"


def test_delete_download_only_inside_downloads(client, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    sid = client.db.insert("sources", kind="youtube", path=str(outside), url="u", title="t", created_at=time.time())
    assert client.delete(f"/api/sources/{sid}?delete_download=true", headers=H).json()["removed_download"] is False
    assert outside.exists()
    d = client.paths.downloads / "abc"
    d.mkdir(parents=True)
    (d / "abc.mp4").write_bytes(b"x")
    sid = client.db.insert("sources", kind="youtube", path=str(d / "abc.mp4"), url="u2", title="t", created_at=time.time())
    assert client.delete(f"/api/sources/{sid}?delete_download=true", headers=H).json()["removed_download"] is True
    assert not d.exists()


def test_clean_refuses_while_processing(client):
    jid = client.db.add_job("process", "heavy", source_id=1)
    client.db.claim_job("heavy", 1)
    assert client.post("/api/storage/clean", json={"what": "work"}, headers=H).status_code == 409
    client.db.update("jobs", jid, status="done")
    assert client.post("/api/storage/clean", json={"what": "work"}, headers=H).status_code == 200


def test_logs_are_redacted(client):
    from clipsift.log import register_secret
    register_secret("EAAverysecrettokenvalue1234567890")
    (client.paths.logs / "worker.log").write_text("call with access_token=EAAverysecrettokenvalue1234567890&x=1\n")
    lines = client.get("/api/logs?name=worker").json()["lines"]
    assert "EAAverysecret" not in "\n".join(lines)
    assert client.get("/api/logs?name=../../etc/passwd").status_code == 400


# ---------- game event feed over HTTP ----------

def test_gsi_endpoint_token_and_events(client):
    tok = client.get("/api/gsi/config/cs2").json()["text"].split('"token" "')[1].split('"')[0]
    payload = lambda k: {"auth": {"token": tok}, "provider": {"steamid": "1"},  # noqa: E731
                         "player": {"steamid": "1", "state": {"round_kills": k, "health": 100}}, "round": {"phase": "live"}}
    plain = TestClient(client.app, base_url="http://127.0.0.1:8765")  # the game has no cookie
    assert plain.post("/gsi/cs2", json={**payload(0), "auth": {"token": "wrong"}}).status_code == 403
    assert plain.post("/gsi/cs2", json=payload(0)).status_code == 200
    assert plain.post("/gsi/cs2", json=payload(2)).status_code == 200
    assert client.db.one("SELECT COUNT(*) AS n FROM game_events WHERE kind='kill'")["n"] == 2
    assert client.get("/api/gsi/status").json()["cs2"]["events_24h"] == 2


def test_gsi_offset_estimate():
    from clipsift.detect.gsi import estimate_offset
    feed = [100.0, 104.0, 230.0, 233.5, 400.0]
    game = [t - 7.0 for t in feed]           # game clock 7 s behind the video
    est = estimate_offset(game, feed)
    assert est and abs(est["offset"] - 7.0) <= 0.5 and est["matched"] == 5
    assert estimate_offset(game, [10.0, 900.0, 1500.0]) is None


# ---------- references and preferences ----------

def test_parse_numbers_and_dates():
    assert parse_number("1,234") == 1234 and parse_number("1.2K") == 1200 and parse_number("3M") == 3_000_000
    assert parse_number("") is None and parse_number("n/a") is None and parse_number("0") == 0
    assert parse_date("2026-01-05") and parse_date("01/05/2026") and parse_date("1767571200") == 1767571200.0
    assert parse_date("not a date") is None


def test_parse_meta_business_suite_csv():
    text = ("﻿Post ID,Permalink,Publish time,Views,Likes,Comments,Shares,Saves,Description\n"
            "1,https://www.instagram.com/reel/A/,2026-10-01 18:00,\"12,400\",900,40,12,30,ace clutch\n"
            "2,https://www.instagram.com/reel/B/,2026-10-03 18:00,,55,,,,funny one\n")
    rows, cols = parse_csv(text)
    assert cols["views"] == "Views" and len(rows) == 2
    assert rows[0]["views"] == 12400 and rows[1]["views"] is None and rows[1]["likes"] == 55


def test_csv_without_link_column_rejected():
    with pytest.raises(ValueError):
        parse_csv("a,b\n1,2\n")


def test_upsert_merges_by_permalink(tmp_path):
    from clipsift.db import DB
    db = DB(tmp_path / "r.db")
    upsert_metrics(db, [{"permalink": "p1", "views": 10, "likes": None}], "csv")
    res = upsert_metrics(db, [{"permalink": "p1", "views": 20, "likes": 3}], "csv")
    assert res == {"created": 0, "updated": 1}
    r = db.one("SELECT * FROM refs WHERE permalink='p1'")
    assert r["views"] == 20 and r["likes"] == 3


def test_missing_metrics_are_not_zero():
    refs = [{"id": 1, "views": 0}, {"id": 2, "views": None}, {"id": 3, "views": 1000}]
    perf = performance(refs)
    assert 2 not in perf and perf[1]["z"] < perf[3]["z"]


def test_age_adjustment_does_not_punish_old_posts():
    now = 1_800_000_000
    refs = [{"id": i, "views": v, "posted_at": now - age * 86400} for i, (v, age) in
            enumerate([(1000, 300), (900, 200), (700, 100), (300, 10), (200, 5), (150, 2)])]
    perf = performance(refs, now=now)
    assert all(p["age_adjusted"] for p in perf.values())
    # once age is accounted for, the newest posts are not simply "worst"
    raw_rank = sorted(perf, key=lambda i: refs[i]["views"])
    adj_rank = sorted(perf, key=lambda i: perf[i]["z"])
    assert raw_rank != adj_rank


def test_suggestions_need_enough_data_and_stay_bounded():
    s = suggest([], [])
    assert "duration" not in s and all(0.75 <= v <= 1.3 for v in s["category_weights"].values())
    refs = [{"id": i, "views": 10 ** (2 + i % 3), "features": {"duration": 20 + i, "category": "funny" if i % 2 else "skill"}}
            for i in range(10)]
    decisions = [{"category": "funny", "decision": "accepted"}] * 8 + [{"category": "skill", "decision": "rejected"}] * 8
    s = suggest(refs, decisions)
    assert s["duration"]["n"] == 10 and 20 <= s["duration"]["target"] <= 29
    assert s["category_weights"]["funny"] > s["category_weights"]["skill"]
    assert all(0.75 <= v <= 1.3 for v in s["category_weights"].values())
    assert any("not causes" in n for n in s["notes"])


# ---------- youtube links ----------

@pytest.mark.parametrize("url,vid", [("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=3s", "jNQXAC9IVRw"),
                                     ("youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
                                     ("https://www.youtube.com/live/jNQXAC9IVRw?si=x", "jNQXAC9IVRw"),
                                     ("https://m.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw")])
def test_normalize_url(url, vid):
    assert normalize_url(url) == f"https://www.youtube.com/watch?v={vid}"


@pytest.mark.parametrize("url", ["", "https://vimeo.com/1", "https://www.youtube.com/@chan", "https://www.youtube.com/watch?v=short",
                                 "https://www.youtube.com/playlist?list=PL1"])
def test_normalize_url_rejects(url):
    with pytest.raises(YTError):
        normalize_url(url)


@pytest.mark.parametrize("msg,kind", [("ERROR: [youtube] x: Private video. Sign in", "private"),
                                      ("Join this channel to get access to members-only content", "members"),
                                      ("Sign in to confirm your age", "age"),
                                      ("The uploader has not made this video available in your country", "geo"),
                                      ("Sign in to confirm you're not a bot", "bot_check"),
                                      ("This video is unavailable", "unavailable"),
                                      ("HTTP Error 429: Too Many Requests", "rate_limited"),
                                      ("Unable to download webpage: timed out", "network")])
def test_classify_errors(msg, kind):
    assert classify(msg).kind == kind


def test_live_and_post_live_refused():
    from clipsift.ingest.youtube import _check_status
    for st, kind in (("is_live", "live"), ("is_upcoming", "upcoming"), ("post_live", "post_live")):
        with pytest.raises(YTError) as e:
            _check_status({"live_status": st})
        assert e.value.kind == kind
    _check_status({"live_status": "was_live", "availability": "public"})
    with pytest.raises(YTError):
        _check_status({"live_status": "was_live", "availability": "subscriber_only"})

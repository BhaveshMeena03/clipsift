"""Local API + UI. Every route is a thin layer over the DB and job queue; heavy work runs in the worker."""
import asyncio
import logging
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__, models
from ..config import DEFAULT_SETTINGS, IS_WINDOWS, PKG_DIR, SECRET_SETTINGS, Paths
from ..db import DB
from ..detect.games import game_list
from ..log import register_secret, tail
from ..media import ffmpeg
from ..media.disk import free_bytes
from ..media.probe import ProbeError, probe
from ..media.tracks import ROLES
from ..util import dir_size, human_bytes
from .security import LocalOnly, load_token, sign

log = logging.getLogger("clipsift.server")
WEB = PKG_DIR / "web"
GSI_PORT = 8765  # what the CS2/Dota config files point at
CAND_STATUSES = {"new", "accepted", "rejected", "exported"}
EDIT_KEYS = {"start", "end", "hook", "hook_seconds", "captions_on", "censor_on", "gameplay_mode", "pan", "facecam_h",
             "facecam_off", "caption_overrides", "caption_y", "auto_insets"}


def create_app(paths: Paths, port_ref: dict | None = None, token: str | None = None, on_shutdown=None) -> FastAPI:
    from contextlib import asynccontextmanager
    paths.ensure()
    db = DB(paths.db)
    port_ref = port_ref if port_ref is not None else {"port": 0}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        # runs inside uvicorn's graceful shutdown, before it re-raises SIGTERM
        if on_shutdown:
            await asyncio.to_thread(on_shutdown)

    app = FastAPI(title="ClipSift", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    token = token or load_token(paths.home)
    app.add_middleware(LocalOnly, port_ref=port_ref, token=token)
    app.state.db = db
    app.state.paths = paths
    st = db.settings()
    ffmpeg.configure(paths.tools, st.get("ffmpeg_path", ""), st.get("ffprobe_path", ""))
    register_secret(st.get("instagram_graph_token", ""))

    def settings() -> dict:
        return db.settings()

    # ---------- pages ----------

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    # ---------- state / system ----------

    @app.get("/api/ping")
    def ping(nonce: str = Query("", max_length=64)):
        # the signed nonce lets a second launch tell this ClipSift from any other program on the port
        out = {"app": "clipsift", "version": __version__}
        if nonce:
            out["proof"] = sign(token, "ping:" + nonce)
        return out

    @app.get("/api/state")
    def state():
        sources = db.all("SELECT * FROM sources ORDER BY id DESC")
        counts = {r["source_id"]: r for r in db.all(
            "SELECT source_id, COUNT(*) AS n, SUM(status='accepted') AS acc, SUM(status='exported') AS exp, "
            "SUM(status='rejected') AS rej FROM candidates GROUP BY source_id")}
        for s in sources:
            s["counts"] = counts.get(s["id"], {"n": 0, "acc": 0, "exp": 0, "rej": 0})
            s.pop("info", None)
        jobs = db.all("SELECT * FROM jobs ORDER BY id DESC LIMIT 60")
        return {"sources": sources, "jobs": jobs, "now": time.time(), "worker": port_ref.get("worker", "unknown")}

    @app.get("/api/system")
    def system():
        from ..gpu import system_info
        info = system_info()
        st = settings()
        try:
            ff = ffmpeg.ffmpeg_bin()
        except ffmpeg.FFmpegError as e:
            ff = None
            info["ffmpeg_error"] = str(e)
        from ..ingest.youtube import deno_path
        info.update({
            "version": __version__, "home": str(paths.home), "ffmpeg": ff, "deno": deno_path(paths, st),
            "hw_encoder": ffmpeg.hw_encoder_label() if ff else None,
            "disk_free": human_bytes(free_bytes(paths.home)),
            "models": [{"name": k, **{x: v.get(x) for x in ("mb", "license", "note", "repo", "convert")},
                        "ready": models.whisper_ready(paths.models, k)} for k, v in models.WHISPER.items()],
            "tagger": {"ready": models.tagger_ready(paths.models), **models.TAGGER["ced-mini"]},
        })
        if port_ref.get("port") not in (None, 0, GSI_PORT):
            info["port_warning"] = (f"Port {GSI_PORT} is used by another program, so CS2/Dota event feeds go to it, "
                                    "not ClipSift. Close that program and restart ClipSift.")
        return info

    @app.get("/api/games")
    def games():
        return game_list()

    # ---------- settings ----------

    @app.get("/api/settings")
    def get_settings():
        st = settings()
        for k in SECRET_SETTINGS:
            st[k] = "set" if st.get(k) else ""
        return st

    @app.put("/api/settings")
    def put_settings(values: dict = Body(...)):
        clean = {}
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                raise HTTPException(400, f"unknown setting {k}")
            if k in SECRET_SETTINGS and v == "set":
                continue
            if k == "gsi_token":
                if v:
                    raise HTTPException(400, "gsi_token is made by ClipSift and cannot be set")
                continue
            clean[k] = clean_setting(k, v)
        if "export_dir" in clean and clean["export_dir"]:
            d = Path(clean["export_dir"]).expanduser()
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise HTTPException(400, f"Cannot use that export folder: {e}") from None
        db.set_settings(clean)
        st = settings()
        ffmpeg.configure(paths.tools, st.get("ffmpeg_path", ""), st.get("ffprobe_path", ""))
        register_secret(st.get("instagram_graph_token", ""))
        return get_settings()

    @app.get("/api/glossary")
    def get_glossary():
        return db.glossary()

    @app.put("/api/glossary")
    def put_glossary(entries: list = Body(...)):
        db.replace_glossary(entries)
        return db.glossary()

    # ---------- layouts ----------

    @app.get("/api/layouts")
    def layouts():
        return db.all("SELECT * FROM layouts ORDER BY name COLLATE NOCASE")

    def _valid_layout(data: dict) -> dict:
        def rect(r):
            if r is None:
                return None
            if not (isinstance(r, list) and len(r) == 4 and all(isinstance(x, (int, float)) for x in r)):
                raise HTTPException(400, "regions must be [x, y, w, h]")
            x, y, w, h = (float(v) for v in r)
            if w <= 0.01 or h <= 0.01 or x < -0.001 or y < -0.001 or x + w > 1.001 or y + h > 1.001:
                raise HTTPException(400, "region is outside the frame or too small")
            return [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]
        out = dict(data)
        out["facecam"] = rect(data.get("facecam"))
        out["gameplay"] = rect(data.get("gameplay"))
        insets = data.get("hud_insets") or []
        if not isinstance(insets, list) or not all(isinstance(i, dict) for i in insets):
            raise HTTPException(400, "hud_insets must be a list")
        out["hud_insets"] = []
        for i in insets:
            if not i.get("src"):
                continue
            pos = i.get("pos", "tr")
            if pos not in ("tr", "tl", "br", "bl", "tc") or not _is_num(i.get("scale", 0.32)):
                raise HTTPException(400, "inset position or size is not valid")
            out["hud_insets"].append({"src": rect(i["src"]), "pos": pos,
                                      "scale": min(0.6, max(0.15, float(i.get("scale", 0.32))))})
        brand = data.get("brand") or {}
        if not isinstance(brand, dict):
            raise HTTPException(400, "brand must be an object")
        out["brand"] = {}
        if not isinstance(brand.get("text", ""), str) or len(brand.get("text", "")) > 80:
            raise HTTPException(400, "brand text must be short text")
        out["brand"]["text"] = brand.get("text", "")
        for key in ("color", "text_color"):
            if brand.get(key):
                if not (isinstance(brand[key], str) and re.fullmatch(r"#[0-9a-fA-F]{6}", brand[key])):
                    raise HTTPException(400, f"brand {key} must look like #1a2b3c")
                out["brand"][key] = brand[key]
        if brand.get("logo"):
            logo = Path(str(brand["logo"]))
            if not logo.is_absolute() or not logo.is_file() or logo.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                raise HTTPException(400, "brand logo must be an existing .png, .jpg or .webp file")
            out["brand"]["logo"] = str(logo)
        hr = {}
        for g, regs in (data.get("hud_regions") or {}).items():
            hr[g] = {k: rect(v) for k, v in regs.items() if k in ("killfeed", "banner", "reward") and v}
        out["hud_regions"] = hr
        if out.get("gameplay_mode") not in (None, "crop", "fit", "full"):
            raise HTTPException(400, "gameplay_mode must be crop, fit or full")
        for k, lo, hi in (("facecam_h", 0.1, 0.5), ("brand_h", 0.0, 0.2)):
            if k in out:
                out[k] = min(hi, max(lo, float(out[k])))
        return out

    @app.post("/api/layouts")
    def create_layout(body: dict = Body(...)):
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "name required")
        if db.one("SELECT id FROM layouts WHERE name=?", (name,)):
            raise HTTPException(409, "a layout with that name exists")
        now = time.time()
        lid = db.insert("layouts", name=name, data_json=_valid_layout(body.get("data") or {}), created_at=now, updated_at=now)
        return db.one("SELECT * FROM layouts WHERE id=?", (lid,))

    @app.put("/api/layouts/{lid}")
    def update_layout(lid: int, body: dict = Body(...)):
        row = db.one("SELECT * FROM layouts WHERE id=?", (lid,))
        if not row:
            raise HTTPException(404, "layout not found")
        fields = {"updated_at": time.time()}
        if "data" in body:
            fields["data_json"] = _valid_layout(body["data"])
        if body.get("name"):
            fields["name"] = str(body["name"]).strip()
        db.update("layouts", lid, **fields)
        return db.one("SELECT * FROM layouts WHERE id=?", (lid,))

    @app.delete("/api/layouts/{lid}")
    def delete_layout(lid: int):
        db.execute("UPDATE sources SET layout_id=NULL WHERE layout_id=?", (lid,))
        db.execute("DELETE FROM layouts WHERE id=?", (lid,))
        return {"ok": True}

    # ---------- sources ----------

    def _source_or_404(sid: int) -> dict:
        s = db.one("SELECT * FROM sources WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404, "source not found")
        return s

    @app.post("/api/sources/local")
    def add_local(body: dict = Body(...)):
        added, errors = [], []
        default_layout = db.one("SELECT id FROM layouts ORDER BY updated_at DESC LIMIT 1")
        for raw in body.get("paths") or []:
            p = Path(str(raw).strip().strip('"')).expanduser()
            if not str(raw).strip():
                continue
            try:
                info = probe(p)
            except (ProbeError, ffmpeg.FFmpegError, OSError) as e:
                errors.append({"path": str(p), "error": str(e)})
                continue
            existing = db.one("SELECT id FROM sources WHERE path=?", (str(p.resolve()),))
            if existing:
                added.append(existing["id"])
                continue
            now = time.time()
            recorded = _recorded_at(p, info)
            sid = db.insert("sources", kind="local", path=str(p.resolve()), title=p.stem, duration=info.duration,
                            width=info.width, height=info.height, fps=info.fps, orientation=info.orientation,
                            info_json=info.to_dict(), status="new", recorded_at=recorded,
                            layout_id=default_layout["id"] if default_layout and info.orientation == "horizontal" else None,
                            created_at=now, updated_at=now)
            added.append(sid)
        return {"added": added, "errors": errors}

    @app.post("/api/pick-files")
    async def pick_files(body: dict = Body(default={})):
        kind = body.get("kind", "video")
        cmd = [sys.executable, "-m", "clipsift.tools.pick_files", "--kind", kind]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=600, **kw)
        if proc.returncode != 0:
            raise HTTPException(500, "The file picker could not open. Paste the file paths instead.")
        import json as _json
        try:
            return {"paths": _json.loads(proc.stdout.strip() or "[]")}
        except ValueError:
            return {"paths": []}

    @app.post("/api/youtube/check")
    async def yt_check(body: dict = Body(...)):
        from ..ingest.youtube import YTError, check
        try:
            return await asyncio.wait_for(asyncio.to_thread(check, body.get("url", ""), paths, settings(),
                                                            body.get("quality") or settings().get("download_quality", "best")), 90)
        except YTError as e:
            return JSONResponse({"error": str(e), "kind": e.kind}, status_code=400)
        except TimeoutError:
            return JSONResponse({"error": "YouTube took too long to answer. Try again.", "kind": "network"}, status_code=504)

    @app.post("/api/sources/youtube")
    def add_youtube(body: dict = Body(...)):
        from ..ingest.youtube import YTError, normalize_url
        try:
            url = normalize_url(body.get("url", ""))
        except YTError as e:
            raise HTTPException(400, str(e)) from None
        existing = db.one("SELECT * FROM sources WHERE url=?", (url,))
        if existing and existing.get("path") and Path(existing["path"]).is_file():
            return {"source_id": existing["id"], "job_id": None, "message": "Already downloaded"}
        now = time.time()
        sid = existing["id"] if existing else db.insert(
            "sources", kind="youtube", url=url, title=body.get("title") or url, status="queued",
            game=body.get("game") or "generic", created_at=now, updated_at=now,
            layout_id=(db.one("SELECT id FROM layouts ORDER BY updated_at DESC LIMIT 1") or {}).get("id"))
        jid = db.add_job("download", "download", source_id=sid,
                         payload={"quality": body.get("quality"), "auto_process": bool(body.get("auto_process", True))})
        return {"source_id": sid, "job_id": jid}

    @app.get("/api/sources/{sid}")
    def get_source(sid: int):
        return _source_or_404(sid)

    @app.patch("/api/sources/{sid}")
    def patch_source(sid: int, body: dict = Body(...)):
        s = _source_or_404(sid)
        fields = {}
        if "game" in body:
            if body["game"] not in {g["id"] for g in game_list()}:
                raise HTTPException(400, "unknown game")
            fields["game"] = body["game"]
        if "layout_id" in body:
            if body["layout_id"] is not None and not db.one("SELECT id FROM layouts WHERE id=?", (body["layout_id"],)):
                raise HTTPException(400, "unknown layout")
            fields["layout_id"] = body["layout_id"]
        if "track_roles" in body:
            n = len((s.get("info") or {}).get("audio") or [])
            roles = {str(k): v for k, v in (body["track_roles"] or {}).items()}
            if any(v not in ROLES or not str(k).isdigit() or int(k) >= n for k, v in roles.items()):
                raise HTTPException(400, f"track roles must be one of {ROLES} for tracks 0..{n - 1}")
            fields["track_roles_json"] = roles
        if "options" in body:
            fields["options_json"] = {**(s.get("options") or {}), **(body["options"] or {})}
        for k in ("recorded_at", "event_offset", "title"):
            if k in body:
                fields[k] = body[k]
        fields["updated_at"] = time.time()
        db.update("sources", sid, **fields)
        return _source_or_404(sid)

    @app.delete("/api/sources/{sid}")
    def delete_source(sid: int, delete_download: bool = False):
        s = _source_or_404(sid)
        for j in db.all("SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running')", (sid,)):
            db.update("jobs", j["id"], cancel_requested=1)
        _remove_temp(sid)
        removed_file = False
        if delete_download and s["kind"] == "youtube" and s.get("path"):
            p = Path(s["path"]).resolve()
            if paths.downloads.resolve() in p.parents:  # only files ClipSift downloaded itself
                import shutil
                shutil.rmtree(p.parent, ignore_errors=True)
                removed_file = True
        db.execute("DELETE FROM candidates WHERE source_id=?", (sid,))
        db.execute("DELETE FROM sources WHERE id=?", (sid,))
        return {"ok": True, "removed_download": removed_file}

    @app.get("/api/sources/{sid}/frame")
    async def frame(sid: int, t: float = 0.0, w: int = 1280):
        from ..media.frames import grab_jpeg
        s = _source_or_404(sid)
        if not s.get("path") or not Path(s["path"]).is_file():
            raise HTTPException(404, "file not available yet")
        t = max(0.0, min(float(s.get("duration") or 0) - 0.5, t))
        try:
            data = await asyncio.to_thread(grab_jpeg, Path(s["path"]), t, max(320, min(1920, w)))
        except RuntimeError as e:
            raise HTTPException(500, str(e)) from None
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/sources/{sid}/process")
    def process(sid: int, body: dict = Body(default={})):
        s = _source_or_404(sid)
        if not s.get("path") or not Path(s["path"]).is_file():
            raise HTTPException(400, "The recording is not on disk yet.")
        if body.get("options"):
            db.update("sources", sid, options_json={**(s.get("options") or {}), **body["options"]})
        active = db.active_job_for("process", source_id=sid)
        if active:
            return {"job_id": active["id"], "already": True}
        last = db.one("SELECT * FROM jobs WHERE kind='process' AND source_id=? ORDER BY id DESC LIMIT 1", (sid,))
        if last and last["status"] in ("cancelled", "failed") and not body.get("fresh"):
            db.update("jobs", last["id"], status="queued", cancel_requested=0, error=None, message="Resuming")
            jid = last["id"]
        else:
            jid = db.add_job("process", "heavy", source_id=sid)
        db.update("sources", sid, status="queued", error=None)
        return {"job_id": jid}

    # ---------- jobs ----------

    @app.post("/api/jobs/{jid}/cancel")
    def cancel(jid: int):
        j = db.job(jid)
        if not j:
            raise HTTPException(404, "job not found")
        if j["status"] == "queued":
            db.update("jobs", jid, status="cancelled", message="Cancelled before it started", finished_at=time.time())
        elif j["status"] == "running":
            db.update("jobs", jid, cancel_requested=1, message="Cancelling...")
        return db.job(jid)

    @app.post("/api/jobs/{jid}/resume")
    def resume(jid: int):
        j = db.job(jid)
        if not j:
            raise HTTPException(404, "job not found")
        if j["status"] not in ("cancelled", "failed"):
            raise HTTPException(400, "only cancelled or failed jobs can be resumed")
        db.update("jobs", jid, status="queued", cancel_requested=0, error=None, crash_count=0, message="Resuming")
        return db.job(jid)

    @app.get("/api/jobs/{jid}")
    def get_job(jid: int):
        j = db.job(jid)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    # ---------- candidates ----------

    @app.get("/api/candidates")
    def candidates(source_id: int | None = None, category: str | None = None, status: str | None = None):
        q, args = "SELECT * FROM candidates WHERE 1=1", []
        if source_id:
            q += " AND source_id=?"
            args.append(source_id)
        if category:
            q += " AND category=?"
            args.append(category)
        if status:
            q += " AND status=?"
            args.append(status)
        from ..pipeline.export import candidate_lines
        rows = db.all(q + " ORDER BY source_id DESC, rank_score DESC", tuple(args))
        srcs = {s["id"]: s for s in db.all("SELECT id, title, kind, url, video_id, orientation, game FROM sources")}
        st = settings()
        out = []
        for c in rows:
            try:
                c["warnings"] = candidate_lines(paths, c, st)["warnings"]
            except Exception:
                c["warnings"] = {}
            c.pop("captions", None)
            c["source"] = srcs.get(c["source_id"])
            out.append(c)
        return out

    @app.get("/api/candidates/{cid}")
    def candidate(cid: int):
        from ..pipeline.export import auto_insets, candidate_lines
        c = db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404, "candidate not found")
        st = settings()
        cl = candidate_lines(paths, c, st)
        c["lines"] = [{k: ln[k] for k in ("abs_start", "abs_end", "start", "end", "text", "display", "status", "hidden",
                                          "edited", "words", "segs")} for ln in cl["lines"]]
        seg_info = (c.get("captions") or {}).get("segs") or {}
        for ln in c["lines"]:
            alts = [seg_info.get(s, {}).get("alt_text") or seg_info.get(s, {}).get("prev_text") for s in ln["segs"]]
            ln["alt"] = next((a for a in alts if a), None)
        c["warnings"] = cl["warnings"]
        c["mute_spans"] = cl["mute_spans"]
        c["bounds"] = [cl["start"], cl["end"]]
        c.pop("captions", None)
        src = db.one("SELECT * FROM sources WHERE id=?", (c["source_id"],))
        c["source"] = {k: src.get(k) for k in ("id", "title", "kind", "url", "video_id", "duration", "orientation", "game")}
        lay = None
        if src.get("layout_id"):
            row = db.one("SELECT * FROM layouts WHERE id=?", (src["layout_id"],))
            lay = row["data"] if row else None
        info = src.get("info") or {}
        c["auto_insets"] = auto_insets(c, lay or {}, src, int(info.get("width") or 1920), int(info.get("height") or 1080)) \
            if src.get("orientation") == "horizontal" else []
        c["preview_ready"] = _preview_path(cid) is not None
        return c

    @app.patch("/api/candidates/{cid}")
    def patch_candidate(cid: int, body: dict = Body(...)):
        c = db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404, "candidate not found")
        fields = {"updated_at": time.time()}
        if "status" in body:
            if body["status"] not in CAND_STATUSES - {"exported"}:
                raise HTTPException(400, "status must be new, accepted or rejected")
            fields["status"] = body["status"]
            if body["status"] in ("accepted", "rejected"):
                db.insert("decisions", candidate_id=cid, source_id=c["source_id"], decision=body["status"],
                          category=c["category"], features_json=_decision_features(c), at=time.time())
        if "edit" in body:
            e = dict(c.get("edit") or {})
            for k, v in (body["edit"] or {}).items():
                if k not in EDIT_KEYS:
                    raise HTTPException(400, f"unknown edit field {k}")
                e[k] = v
            src = db.one("SELECT duration FROM sources WHERE id=?", (c["source_id"],))
            dur = float(src.get("duration") or 1e9)
            s0 = float(e["start"]) if e.get("start") is not None else c["start"]
            s1 = float(e["end"]) if e.get("end") is not None else c["end"]
            s0, s1 = max(0.0, s0), min(dur, s1)
            if s1 - s0 < 1.0:
                raise HTTPException(400, "clip must be at least 1 second")
            if s1 - s0 > 600:
                raise HTTPException(400, "clip can be at most 10 minutes")
            if e.get("start") is not None:
                e["start"] = round(s0, 3)
            if e.get("end") is not None:
                e["end"] = round(s1, 3)
            if e.get("hook") is not None and len(str(e["hook"])) > 120:
                raise HTTPException(400, "hook text is too long (120 characters max)")
            fields["edit_json"] = e
        db.update("candidates", cid, **fields)
        return candidate(cid)

    def _decision_features(c: dict) -> dict:
        ev = c.get("evidence") or []
        return {"category": c["category"], "duration": c["end"] - c["start"], "rank_score": c.get("rank_score"),
                "score": c.get("score"), "kinds": sorted({e["kind"] for e in ev}),
                "tiers": sorted({e["tier"] for e in ev})}

    @app.post("/api/candidates/{cid}/preview")
    def preview(cid: int):
        if not db.one("SELECT id FROM candidates WHERE id=?", (cid,)):
            raise HTTPException(404, "candidate not found")
        active = db.active_job_for("preview", candidate_id=cid)
        if active:
            return {"job_id": active["id"]}
        return {"job_id": db.add_job("preview", "light", candidate_id=cid)}

    @app.post("/api/export")
    def export(body: dict = Body(...)):
        ids = [int(i) for i in body.get("candidate_ids") or []]
        if not ids:
            raise HTTPException(400, "select at least one clip")
        jobs = []
        for cid in ids:
            c = db.one("SELECT id, source_id FROM candidates WHERE id=?", (cid,))
            if not c:
                continue
            active = db.active_job_for("export", candidate_id=cid)
            jobs.append(active["id"] if active else db.add_job("export", "light", candidate_id=cid, source_id=c["source_id"]))
        return {"job_ids": jobs}

    # ---------- media ----------

    def _file(path: str | None, media_type="video/mp4"):
        if not path or not Path(path).is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.get("/media/proxy/{cid}")
    def media_proxy(cid: int):
        c = db.one("SELECT proxy_path FROM candidates WHERE id=?", (cid,))
        return _file(c and c["proxy_path"])

    def _preview_path(cid: int) -> Path | None:
        """Only a preview rendered from the current edits counts."""
        from ..pipeline.export import build_spec, spec_key
        try:
            spec, _c, _s = build_spec(db, paths, settings(), cid, preview=True)
        except RuntimeError:
            return None
        p = paths.previews / f"cand_{cid}_{spec_key(spec)}.mp4"
        return p if p.is_file() else None

    @app.get("/media/preview/{cid}")
    def media_preview(cid: int):
        p = _preview_path(cid)
        return _file(str(p) if p else None)

    @app.get("/media/export/{cid}")
    def media_export(cid: int):
        c = db.one("SELECT export_path FROM candidates WHERE id=?", (cid,))
        return _file(c and c["export_path"])

    # ---------- storage / logs ----------

    def _remove_temp(sid: int):
        import shutil
        shutil.rmtree(paths.source_work(sid), ignore_errors=True)
        for c in db.all("SELECT id FROM candidates WHERE source_id=?", (sid,)):
            for p in paths.previews.glob(f"cand_{c['id']}_*.mp4"):
                p.unlink(missing_ok=True)
            db.update("candidates", c["id"], proxy_path=None)

    @app.get("/api/storage")
    def storage():
        rows = []
        for s in db.all("SELECT id, title, kind, path FROM sources ORDER BY id DESC"):
            dl = 0
            if s["kind"] == "youtube" and s.get("path") and Path(s["path"]).is_file():
                dl = Path(s["path"]).stat().st_size
            rows.append({"id": s["id"], "title": s["title"], "work": dir_size(paths.source_work(s["id"])),
                         "download": dl})
        totals = {"work": dir_size(paths.work), "previews": dir_size(paths.previews), "downloads": dir_size(paths.downloads),
                  "exports": dir_size(Path(settings().get("export_dir") or paths.exports)), "models": dir_size(paths.models),
                  "logs": dir_size(paths.logs), "free": free_bytes(paths.home)}
        return {"sources": rows, "totals": totals, "human": {k: human_bytes(v) for k, v in totals.items()}}

    @app.post("/api/storage/clean")
    def clean(body: dict = Body(...)):
        import shutil
        what = body.get("what")
        busy = db.one("SELECT id FROM jobs WHERE status='running' AND kind IN ('process','preview')")
        if what in ("work", "all_temp") and busy:
            raise HTTPException(409, "Wait for processing to finish (or cancel it) before clearing work files.")
        if what in ("previews", "all_temp"):
            shutil.rmtree(paths.previews, ignore_errors=True)
            paths.previews.mkdir(parents=True, exist_ok=True)
        if what in ("work", "all_temp"):
            shutil.rmtree(paths.work, ignore_errors=True)
            paths.work.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(paths.home / "render_work", ignore_errors=True)
            db.execute("UPDATE candidates SET proxy_path=NULL")
        if what not in ("previews", "work", "all_temp"):
            raise HTTPException(400, "what must be previews, work or all_temp")
        return storage()

    @app.get("/api/logs")
    def logs(name: str = Query("worker"), lines: int = 300):
        if name not in ("worker", "server"):
            raise HTTPException(400, "unknown log")
        return {"lines": tail(paths.logs / f"{name}.log", max(10, min(2000, lines))), "path": str(paths.logs)}

    @app.post("/api/open")
    def open_folder(body: dict = Body(...)):
        what = body.get("what")
        target = {"exports": Path(settings().get("export_dir") or paths.exports), "logs": paths.logs,
                  "home": paths.home, "downloads": paths.downloads}.get(what)
        if what == "file":
            c = db.one("SELECT export_path FROM candidates WHERE id=?", (int(body.get("candidate_id", 0)),))
            target = Path(c["export_path"]).parent if c and c.get("export_path") else None
        if not target or not target.exists():
            raise HTTPException(404, "folder not found")
        if IS_WINDOWS:
            os.startfile(str(target))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True}

    @app.post("/api/models/download")
    def model_download(body: dict = Body(...)):
        name = body.get("name")
        if name != "ced-mini" and name not in models.WHISPER:
            raise HTTPException(400, "unknown model")
        if name in models.WHISPER and models.WHISPER[name].get("convert"):
            raise HTTPException(400, "This model needs the one-time conversion. Run setup\\convert_models.ps1 (see docs/MODELS.md).")
        return {"job_id": db.add_job("models", "download", payload={"name": name})}

    # ---------- references, preferences, instagram, game feeds ----------
    from .extra import register_extra
    register_extra(app, db, paths, settings)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": "Something went wrong. Details are in the server log."}, status_code=500)

    return app


SETTING_CHOICES = {
    "language_mode": {"mixed", "en", "hi", "auto"}, "asr_device": {"auto", "cuda", "cpu"},
    "visual_scan": {"auto", "keyframes", "1fps", "off"}, "encoder": {"auto", "nvenc", "amf", "qsv", "x264"},
    "export_quality": {"max", "fast"}, "download_quality": {"best", "1080", "720"},
    "asr_model_gpu": set(models.WHISPER), "asr_model_cpu": set(models.WHISPER),
}
SETTING_RANGES = {
    "max_candidates": (5, 200), "min_clip_s": (5, 120), "max_clip_s": (10, 180), "quiet_samples_per_hour": (0, 60),
    "caption_words_max": (1, 8), "caption_y": (0.3, 0.9), "vertical_caption_y": (0.3, 0.9), "hook_y": (0.05, 0.9),
    "vertical_hook_y": (0.05, 0.9), "hook_seconds": (0, 60), "asr_batch_size": (0, 64),
}
TOOL_SETTINGS = {"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe", "deno_path": "deno"}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def clean_setting(k: str, v):
    """Every setting keeps the type of its default, enums stay in their list and numbers in range, so a
    setting can never smuggle markup into the page or point ClipSift at an arbitrary program."""
    default = DEFAULT_SETTINGS[k]

    def bad(why: str):
        raise HTTPException(400, f"{k}: {why}")

    if k in TOOL_SETTINGS:
        if not isinstance(v, str):
            bad("must be a path")
        if v:
            p = Path(v)
            exe = TOOL_SETTINGS[k] + (".exe" if IS_WINDOWS else "")
            if not p.is_absolute() or not p.is_file() or p.name.lower() != exe:
                bad(f"must be the full path of an existing {exe}")
        return v
    if k == "pref_duration":
        if v is not None and not isinstance(v, dict):
            bad("must be empty or a duration preference")
        return v
    if k == "pref_category_weights":
        if not isinstance(v, dict) or not all(isinstance(a, str) and _is_num(b) for a, b in v.items()):
            bad("must map categories to numbers")
        return v
    if isinstance(default, bool):
        if not isinstance(v, bool):
            bad("must be true or false")
        return v
    if isinstance(default, (int, float)):
        if not _is_num(v):
            bad("must be a number")
        lo, hi = SETTING_RANGES.get(k, (v, v))
        v = min(hi, max(lo, v))
        return int(round(v)) if isinstance(default, int) else float(v)
    if isinstance(default, list):
        if not isinstance(v, list) or len(v) > 200 or not all(isinstance(x, str) and len(x) <= 100 for x in v):
            bad("must be a list of short words")
        return v
    if not isinstance(v, str) or len(v) > 2000:
        bad("must be text")
    if k in SETTING_CHOICES and v not in SETTING_CHOICES[k]:
        bad(f"must be one of {', '.join(sorted(SETTING_CHOICES[k]))}")
    if k == "instagram_graph_user_id" and v and not v.isdigit():
        bad("must be digits")
    return v


def _recorded_at(p: Path, info) -> float | None:
    """Wall-clock start of a local recording, for lining up game events. OBS names files by start time."""
    import re
    from datetime import datetime
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})-(\d{2})-(\d{2})", p.name)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups())).timestamp()
        except ValueError:
            pass
    if info.creation_time:
        try:
            return datetime.fromisoformat(info.creation_time.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    try:
        st = p.stat()
        born = getattr(st, "st_birthtime", None) or (st.st_ctime if IS_WINDOWS else None)
        return float(born) if born else None
    except OSError:
        return None


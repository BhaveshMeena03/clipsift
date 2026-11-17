"""References, preferences, Instagram metrics and game event feeds."""
import asyncio
import secrets
import time
from pathlib import Path

from fastapi import Body, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..detect import gsi
from ..media.probe import ProbeError, probe
from ..prefs import instagram, references
from ..prefs.model import performance, suggest


def register_extra(app, db, paths, settings):
    states = {"cs2": gsi.CS2State(), "dota2": gsi.Dota2State()}

    # ---------- references ----------

    @app.get("/api/references")
    def list_refs():
        refs = db.all("SELECT * FROM refs ORDER BY COALESCE(posted_at, created_at) DESC")
        perf = performance(refs)
        for r in refs:
            r["perf"] = perf.get(r["id"])
        return refs

    @app.post("/api/references/import")
    def import_refs(body: dict = Body(...)):
        added, errors = [], []
        for raw in body.get("paths") or []:
            p = Path(str(raw).strip().strip('"')).expanduser()
            try:
                probe(p)
            except (ProbeError, OSError) as e:
                errors.append({"path": str(p), "error": str(e)})
                continue
            row = db.one("SELECT id FROM refs WHERE path=?", (str(p.resolve()),))
            if row:
                added.append(row["id"])
                continue
            added.append(db.insert("refs", path=str(p.resolve()), title=p.stem, created_at=time.time()))
        job = db.add_job("reference", "heavy", payload={"ref_ids": added}) if added else None
        return {"added": added, "errors": errors, "job_id": job}

    @app.patch("/api/references/{rid}")
    def patch_ref(rid: int, body: dict = Body(...)):
        if not db.one("SELECT id FROM refs WHERE id=?", (rid,)):
            raise HTTPException(404, "reference not found")
        fields = {}
        for k in ("views", "likes", "comments", "shares", "saves"):
            if k in body:
                v = body[k]
                fields[k] = None if v in (None, "") else references.parse_number(v)
                if v not in (None, "") and fields[k] is None:
                    raise HTTPException(400, f"{k} must be a number")
        if "posted_at" in body:
            fields["posted_at"] = references.parse_date(body["posted_at"]) if body["posted_at"] else None
        for k in ("favourite", "rejected"):
            if k in body:
                fields[k] = 1 if body[k] else 0
        for k in ("title", "permalink", "notes"):
            if k in body:
                fields[k] = body[k]
        if any(k in fields for k in ("views", "likes", "comments", "shares", "saves")):
            fields.update(metrics_source="manual", metrics_at=time.time())
        db.update("refs", rid, **fields)
        return db.one("SELECT * FROM refs WHERE id=?", (rid,))

    @app.delete("/api/references/{rid}")
    def delete_ref(rid: int):
        db.execute("DELETE FROM refs WHERE id=?", (rid,))
        return {"ok": True}

    @app.post("/api/references/csv")
    async def refs_csv(file: UploadFile):
        data = await file.read()
        if len(data) > 5_000_000:
            raise HTTPException(400, "CSV is too large (5 MB max)")
        try:
            rows, cols = references.parse_csv(data.decode("utf-8-sig", errors="replace"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        res = references.upsert_metrics(db, rows, "csv")
        return {**res, "columns": cols, "rows": len(rows)}

    @app.post("/api/references/analyze")
    def refs_analyze(body: dict = Body(default={})):
        return {"job_id": db.add_job("reference", "heavy", payload={"ref_ids": body.get("ref_ids")})}

    @app.get("/api/preferences")
    def prefs():
        refs = db.all("SELECT * FROM refs")
        decisions = db.all("SELECT * FROM decisions")
        st = settings()
        return {"suggestions": suggest(refs, decisions),
                "current": {"use_preferences": st.get("use_preferences"), "category_weights": st.get("pref_category_weights"),
                            "duration": st.get("pref_duration"), "caption_words_max": st.get("caption_words_max"),
                            "caption_upper": st.get("caption_upper")}}

    @app.post("/api/preferences/apply")
    def prefs_apply(body: dict = Body(...)):
        s = suggest(db.all("SELECT * FROM refs"), db.all("SELECT * FROM decisions"))
        vals = {}
        parts = set(body.get("parts") or [])
        if "category_weights" in parts:
            vals["pref_category_weights"] = s["category_weights"]
        if "duration" in parts and s.get("duration"):
            vals["pref_duration"] = s["duration"]
        if "captions" in parts and s.get("captions"):
            vals["caption_words_max"] = max(1, min(8, int(s["captions"]["words_per_line"])))
            vals["caption_upper"] = bool(s["captions"]["upper"])
        if not vals:
            raise HTTPException(400, "nothing to apply yet")
        db.set_settings(vals)
        return prefs()

    # ---------- instagram ----------

    @app.post("/api/instagram/graph-fetch")
    async def ig_graph():
        st = db.settings()
        try:
            rows = await asyncio.to_thread(instagram.graph_fetch, st.get("instagram_graph_user_id", ""),
                                           st.get("instagram_graph_token", ""))
        except instagram.InstagramError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {**references.upsert_metrics(db, rows, "graph"), "rows": len(rows)}

    @app.post("/api/instagram/public")
    async def ig_public(body: dict = Body(...)):
        results = []
        for url in (body.get("urls") or [])[:50]:
            try:
                row = await asyncio.wait_for(asyncio.to_thread(instagram.public_post_meta, url.strip()), 45)
                references.upsert_metrics(db, [row], "public")
                results.append({"url": url, "ok": True, "views": row.get("views"), "likes": row.get("likes")})
            except (TimeoutError, instagram.InstagramError) as e:
                results.append({"url": url, "ok": False, "error": str(e) or "timed out"})
        return {"results": results}

    # ---------- game event feeds (CS2 / Dota 2 GSI) ----------

    def gsi_token() -> str:
        st = db.settings()
        tok = st.get("gsi_token")
        if not tok:
            tok = secrets.token_hex(16)
            db.set_settings({"gsi_token": tok})
        return tok

    @app.post("/gsi/{game}")
    async def gsi_in(game: str, request: Request):
        if game not in states:
            return JSONResponse({"error": "unknown game"}, status_code=404)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not isinstance(payload, dict) or (payload.get("auth") or {}).get("token") != gsi_token():
            return JSONResponse({"error": "bad token"}, status_code=403)
        events = states[game].update(payload, now=time.time())
        if events:
            gsi.store(db, game, events)
        return {"ok": True}

    @app.get("/api/gsi/status")
    def gsi_status():
        out = {}
        for g in states:
            last = db.one("SELECT MAX(t_wall) AS t, COUNT(*) AS n FROM game_events WHERE game=? AND t_wall > ?",
                          (g, time.time() - 86400))
            out[g] = {"events_24h": last["n"], "last_event": last["t"]}
        return out

    @app.get("/api/gsi/config/{game}")
    def gsi_config(game: str, request: Request):
        if game not in states:
            raise HTTPException(404, "unknown game")
        port = request.url.port or 8765
        return {"text": gsi.config_text(game, port, gsi_token()), **gsi.install_target(game)}

    @app.post("/api/gsi/install/{game}")
    def gsi_install(game: str, request: Request):
        if game not in states:
            raise HTTPException(404, "unknown game")
        target = gsi.install_target(game)
        if not target.get("path"):
            raise HTTPException(404, target.get("hint") or "Could not find the game folder. Copy the file by hand.")
        port = request.url.port or 8765
        Path(target["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(target["path"]).write_text(gsi.config_text(game, port, gsi_token()), encoding="utf-8")
        return {"ok": True, "path": target["path"], "note": target.get("note")}

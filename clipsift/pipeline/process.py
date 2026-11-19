"""Processing job for one source: probe -> audio scan -> transcribe -> visual scan -> candidates
-> visual detail -> caption check -> proxies. Every stage resumes from its checkpoints."""
import bisect
import logging
import os
import time
from pathlib import Path

from .. import models
from ..analysis import asr
from ..analysis.audio_scan import run_audio_scan
from ..analysis.candidates import find_candidates, quiet_windows
from ..analysis.visual_scan import OcrEngine, hud_config, run_visual_scan, scan_range
from ..detect import gsi
from ..detect.games import game
from ..media import tracks
from ..media.audio import load_audio
from ..media.probe import keyframe_interval, probe
from ..text.glossary import Glossary
from ..text.hooks import hook_options, sentences
from ..util import atomic_write_json, file_fingerprint, read_json
from . import verify
from .chunks import has_speech, plan_chunks, stitch
from .context import JobContext, stage_dir
from .proxies import make_proxy

log = logging.getLogger(__name__)

STAGES = [("probe", 1), ("audio_scan", 8), ("transcribe", 45), ("visual_scan", 22), ("candidates", 1),
          ("visual_detail", 10), ("verify_captions", 8), ("proxies", 5)]
CAPTION_MARGIN = 20.0


def speech_overlap(seg: dict, speech: list, starts: list) -> float:
    a, b = seg["start"], seg["end"]
    if b <= a:
        return 1.0
    i = max(0, bisect.bisect_right(starts, a) - 1)
    tot = 0.0
    while i < len(speech) and speech[i][0] < b:
        tot += max(0.0, min(b, speech[i][1]) - max(a, speech[i][0]))
        i += 1
    return tot / (b - a)


def resegment(segs: list[dict], max_gap: float = 1.2, max_len: float = 12.0) -> list[dict]:
    """Whisper can return one segment across a long silence. Split on word gaps so each
    segment is one stretch of speech."""
    out = []
    for seg in segs:
        words = seg.get("words") or []
        if len(words) < 2:
            out.append(seg)
            continue
        groups, cur = [], [words[0]]
        for w in words[1:]:
            if w["start"] - cur[-1]["end"] > max_gap or (w["end"] - cur[0]["start"] > max_len
                                                          and cur[-1]["text"].rstrip()[-1:] in ".?!,"):
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        groups.append(cur)
        if len(groups) == 1:
            out.append(seg)
            continue
        for g in groups:
            s = dict(seg)
            s["words"] = g
            s["text"] = "".join(w["text"] for w in g).strip()
            s["start"], s["end"] = g[0]["start"], g[-1]["end"]
            out.append(s)
    return out


def word_speech_fraction(words: list[dict], speech: list, starts: list, pad: float = 0.3) -> float:
    if not words:
        return 1.0
    inside = 0
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        i = max(0, bisect.bisect_right(starts, mid + pad) - 1)
        if any(speech[j][0] - pad <= mid <= speech[j][1] + pad for j in range(max(0, i - 1), min(len(speech), i + 2))):
            inside += 1
    return inside / len(words)


def postprocess_transcript(segs: list[dict], speech: list, glossary: Glossary) -> dict:
    starts = [s[0] for s in speech]
    words = []
    segs = resegment(segs)
    for k, seg in enumerate(segs):
        seg["id"] = f"s{k}"
        seg["words"] = glossary.apply_words(seg.get("words", []))
        if seg["words"]:
            seg["text"] = "".join(w["text"] for w in seg["words"]).strip()
        else:
            seg["text"] = glossary.apply_text(seg.get("text", ""))
        if not speech:
            ov = None
        elif seg["words"]:
            ov = word_speech_fraction(seg["words"], speech, starts)
        else:
            ov = speech_overlap(seg, speech, starts)
        seg["speech_overlap"] = None if ov is None else round(ov, 3)
        seg["status"] = asr.segment_status(seg, ov)
        for w in seg["words"]:
            w["seg"] = seg["id"]
            words.append(w)
    words.sort(key=lambda w: (w["start"], w["end"]))  # Whisper segments can overlap slightly
    return {"segments": segs, "words": words}


def seg_status_map(transcript: dict) -> dict:
    return {s["id"]: s.get("status", "ok") for s in transcript["segments"]}


def run_process(ctx: JobContext) -> dict:
    db, paths, st = ctx.db, ctx.paths, ctx.settings
    src = db.one("SELECT * FROM sources WHERE id=?", (ctx.job["source_id"],))
    if not src:
        raise RuntimeError("Source was removed.")
    path = Path(src["path"] or "")
    if not path.is_file():
        raise RuntimeError(f"Recording not found: {path}")
    opts = {**{k: st.get(k) for k in ("max_candidates", "min_clip_s", "max_clip_s", "language_mode",
                                       "quiet_samples_per_hour", "visual_scan")}, **(src.get("options") or {})}
    work = paths.source_work(src["id"])
    work.mkdir(parents=True, exist_ok=True)
    db.update("sources", src["id"], status="processing", error=None, updated_at=time.time())

    # probe
    ctx.progress("probe", 0.0, "Reading the file")
    info = probe(path)
    fp = file_fingerprint(path)
    db.update("sources", src["id"], duration=info.duration, width=info.width, height=info.height, fps=info.fps,
              orientation=info.orientation, fingerprint=fp, info_json=info.to_dict())
    tp = tracks.plan_tracks(src.get("track_roles") or {}, len(info.audio))
    if not info.audio:
        ctx.note("This file has no audio. Speech and sound detection are skipped.")
    ctx.progress("probe", 1.0)

    # audio scan
    ctx.check()
    ctx.progress("audio_scan", 0.0, "Scanning audio")
    tagger = None
    use_tagger = bool(st.get("use_audio_tagger")) and bool(info.audio)
    if use_tagger and not models.tagger_ready(paths.models):
        ctx.note("Audio event model not downloaded; laughter/scream detection is off for this run.")
        use_tagger = False
    adir = stage_dir(work, "audio", {"fp": fp, "analysis": tp["analysis"], "voice": tp["voice"], "tagger": use_tagger})
    if info.audio:
        if use_tagger:
            from ..analysis.tagger import Tagger
            tagger = Tagger(paths.models, threads=max(1, (os.cpu_count() or 4) // 2))
        audio = run_audio_scan(path, info.duration, adir, all_tracks=tp["analysis"], voice_tracks=tp["voice"],
                               tagger=tagger, cancel=ctx.cancelled,
                               on_progress=lambda p: ctx.progress("audio_scan", p, "Scanning audio"))
        tagger = None
    else:
        import numpy as np
        n10 = int(info.duration * 10) + 1
        audio = {"rms_all": np.full(n10, -90.0), "rms_voice": np.full(n10, -90.0), "flux": np.zeros(n10),
                 "tags": {}, "has_tags": False, "speech": []}

    # transcribe
    ctx.check()
    ctx.progress("transcribe", 0.0, "Transcribing")
    glossary = Glossary(db.glossary())
    lang = opts.get("language_mode") or "mixed"
    plan = asr.plan_asr(st, paths.models)
    tdir = stage_dir(work, "transcript", {"fp": fp, "voice": tp["voice"], "lang": lang,
                                          "models": [st.get("asr_model_gpu"), st.get("asr_model_cpu")]})
    # rebuilt from chunk checkpoints every run (cheap); only decoding is cached
    transcriber = None
    chunks = plan_chunks(info.duration, audio["speech"])
    results = []
    total = sum(c["end"] - c["start"] for c in chunks) or 1.0
    done = 0.0
    for ch in chunks:
        ctx.check()
        f = tdir / f"chunk_{ch['i']:04d}.json"
        data = read_json(f)
        if data is None:
            if not info.audio or not has_speech(ch, audio["speech"]):
                data = {"segs": [], "skipped": True}
            else:
                if transcriber is None:
                    ctx.progress("transcribe", done / total, f"Loading speech model ({plan.model}, {plan.reason})")
                    transcriber = asr.Transcriber(plan, paths.models, cpu_model=st.get("asr_model_cpu", "small"))
                    if transcriber.plan.device != plan.device:
                        ctx.note(transcriber.plan.reason)
                pcm = load_audio(path, ch["start"], ch["end"] - ch["start"], tp["voice"], cancel=ctx.cancelled)
                base = done

                def prog(p, base=base, ch=ch, tr=transcriber):
                    ctx.progress("transcribe", (base + p * (ch["end"] - ch["start"])) / total,
                                 f"Transcribing {fmt_range(ch)} ({tr.plan.model} on {tr.plan.device})")
                segs = transcriber.transcribe(pcm, offset=ch["start"], language_mode=lang,
                                              prompt=glossary.prompt(), cancel=ctx.cancelled, on_progress=prog,
                                              seg_prefix=f"c{ch['i']}_")
                data = {"segs": segs, "plan": transcriber.plan.__dict__}
            atomic_write_json(f, data)
        results.append(data["segs"])
        done += ch["end"] - ch["start"]
        ctx.progress("transcribe", done / total)
    transcript = postprocess_transcript(stitch(chunks, results), audio["speech"], glossary)
    atomic_write_json(tdir / "transcript.json", transcript)
    ctx.progress("transcribe", 1.0)

    # visual scan
    ctx.check()
    ctx.progress("visual_scan", 0.0, "Scanning video")
    layout = db.one("SELECT * FROM layouts WHERE id=?", (src.get("layout_id"),)) if src.get("layout_id") else None
    layout_data = (layout or {}).get("data") or {}
    regions = {"facecam": layout_data.get("facecam") if info.orientation == "horizontal" else None,
               "gameplay": layout_data.get("gameplay") if info.orientation == "horizontal" else None}
    game_id = src.get("game") or "generic"
    hud_cfg = hud_config(game_id, layout_data) if info.orientation == "horizontal" else {}
    names = [n for n in (st.get("ingame_names") or []) if n]
    vmode = opts.get("visual_scan") or "auto"
    visual = None
    ocr = None
    if hud_cfg:
        try:
            ocr = OcrEngine()
        except Exception as e:
            ctx.note(f"On-screen text reader unavailable ({e}); kill feed reading is off.")
            hud_cfg = {}
    if vmode != "off":
        kf = keyframe_interval(path)
        if vmode == "keyframes" or (vmode == "auto" and kf is not None and kf <= 2.5):
            fps, kfo, how = None, True, f"keyframes (about every {kf:.1f} s)" if kf else "keyframes"
        else:
            fps, kfo, how = 1.0, False, "1 frame per second"
        vdir = stage_dir(work, "visual", {"fp": fp, "fps": fps, "kfo": kfo, "regions": regions, "hud": hud_cfg,
                                          "names": names, "game": game_id})
        visual = run_visual_scan(path, info.duration, vdir, fps=fps, keyframes_only=kfo, regions=regions,
                                 hud_cfg=hud_cfg, ocr=ocr, names=names, game_id=game_id, cancel=ctx.cancelled,
                                 on_progress=lambda p: ctx.progress("visual_scan", p, f"Scanning video, {how}"))
    ctx.progress("visual_scan", 1.0)

    events = list((visual or {}).get("events", []))
    game_events = align_game_events(ctx, src, info.duration, events)
    if any(e["kind"] == "kill" for e in game_events):
        # the game's own count wins; don't count the same kills again from OCR
        events = [e for e in events if not (e.get("src") == "killfeed" and e.get("kind") in ("kill", "kill?"))]
    events += game_events

    # first pass: seeds for the expensive look
    ctx.progress("candidates", 0.2, "Finding candidates")
    max_c = int(opts.get("max_candidates") or 40)
    min_len, max_len = float(opts.get("min_clip_s") or 15), float(opts.get("max_clip_s") or 60)
    prefs = preferences(st)
    seeds = find_candidates(info.duration, audio, transcript, visual, events, game_id=game_id,
                            max_candidates=max_c, min_len=min_len, max_len=max_len, prefs=prefs,
                            orientation=info.orientation, seed_factor=1.5)
    ctx.progress("candidates", 1.0)

    # expensive visual pass on candidate windows plus quiet stretches
    ctx.check()
    ctx.progress("visual_detail", 0.0, "Looking closer at candidate and quiet stretches")
    windows = [(max(0.0, c["start"]), min(info.duration, c["end"])) for c in seeds]
    quiet = quiet_windows(info.duration, audio, visual, int(opts.get("quiet_samples_per_hour") or 0), avoid=windows)
    windows = merge_windows(windows + quiet)
    detail_events: list[dict] = []
    detail_vis = {"t": [], "motion": [], "bright": [], "cut": [], "face": [], "gp": []}
    if vmode != "off" and windows:
        ddir = stage_dir(work, "detail", {"fp": fp, "regions": regions, "hud": hud_cfg, "names": names, "game": game_id})
        tot = sum(b - a for a, b in windows) or 1.0
        done = 0.0
        for a, b in windows:
            ctx.check()
            f = ddir / f"w_{int(a * 10)}_{int(b * 10)}.json"
            data = read_json(f)
            if data is None:
                data = scan_range(path, a, b, fps=2.0, keyframes_only=False, regions=regions, hud_cfg=hud_cfg,
                                  ocr=ocr, names=names, game_id=game_id, cancel=ctx.cancelled)
                atomic_write_json(f, data)
            detail_events += data["events"]
            for k in detail_vis:
                detail_vis[k] += data[k]
            done += b - a
            ctx.progress("visual_detail", done / tot, f"Looking closer at {len(windows)} stretches ({len(quiet)} quiet)")
    ocr = None
    all_events = dedupe_events(events + detail_events)
    merged_vis = merge_visual(visual, detail_vis)
    ctx.progress("visual_detail", 1.0)

    final = find_candidates(info.duration, audio, transcript, merged_vis, all_events, game_id=game_id,
                            max_candidates=max_c, min_len=min_len, max_len=max_len, prefs=prefs,
                            orientation=info.orientation)
    atomic_write_json(work / "candidates.json", {"candidates": final, "events": all_events,
                                                  "quiet_windows": quiet, "game": game_id})

    # caption check on what will actually be exported
    ctx.check()
    ctx.progress("verify_captions", 0.0, "Checking unclear captions")
    vdir = stage_dir(work, "verify", {"fp": fp, "lang": lang, "voice": tp["voice"]})
    transcript = verify.verify_windows(ctx, path, transcript, [(c["start"], c["end"]) for c in final], vdir,
                                       plan=plan, voice_tracks=tp["voice"], lang=lang, glossary=glossary,
                                       transcriber=transcriber,
                                       on_progress=lambda p: ctx.progress("verify_captions", p, "Checking unclear captions"))
    if transcriber is not None:
        transcriber.close()
    atomic_write_json(tdir / "transcript.json", transcript)
    ctx.progress("verify_captions", 1.0)

    ids = save_candidates(db, src["id"], final, transcript, st)

    # proxies for quick preview
    ctx.progress("proxies", 0.0, "Making previews")
    pdir = work / "proxies"
    need = [r["id"] for r in db.all("SELECT id, proxy_path FROM candidates WHERE source_id=?", (src["id"],))
            if r["id"] in ids or not r.get("proxy_path") or not Path(r["proxy_path"]).is_file()]
    for k, cid in enumerate(need):
        ctx.check()
        c = db.one("SELECT * FROM candidates WHERE id=?", (cid,))
        ps = round(max(0.0, c["start"] - 15), 2)
        pe = round(min(info.duration, c["end"] + 15), 2)
        # named by the span, not the row id: ids get reused after re-processing
        out = pdir / f"p_{int(ps * 100)}_{int(pe * 100)}.mp4"
        if not out.is_file():
            make_proxy(path, ps, pe, out, tp["export"], cancel=ctx.cancelled)
        db.update("candidates", cid, proxy_path=str(out), proxy_start=ps)
        ctx.progress("proxies", (k + 1) / max(1, len(need)), f"Making previews {k + 1}/{len(need)}")
    db.update("sources", src["id"], status="ready", updated_at=time.time())
    note = " ".join(ctx.notes[-3:])
    return {"candidates": len(ids), "notes": ctx.notes, "asr": plan.__dict__, "message": note,
            "timings": ctx.finish_timings(), "duration": info.duration}


def align_game_events(ctx, src: dict, duration: float, ocr_events: list[dict]) -> list[dict]:
    db = ctx.db
    if not src.get("recorded_at"):
        return []
    if src.get("event_offset") is None:
        raw = gsi.events_for_source(db, dict(src, event_offset=0.0), duration)
        kills = [e["t"] for e in raw if e["kind"] == "kill"]
        if not raw:
            return []
        feed = [e["t"] for e in ocr_events if e.get("src") == "killfeed" and e.get("kind") in ("kill", "kill?")]
        est = gsi.estimate_offset(kills, feed) if kills else None
        if est:
            off = -est["offset"]
            ctx.note(f"Lined up {est['matched']} of {est['of']} game-reported kills with the kill feed "
                     f"(clock shift {est['offset']:+.1f} s).")
        else:
            off = 0.0
            ctx.note("Game events placed by clock time only; there were no kill feed reads to confirm the timing.")
        db.update("sources", src["id"], event_offset=off)
        src = dict(src, event_offset=off)
    return gsi.events_for_source(db, src, duration)


def fmt_range(ch: dict) -> str:
    from ..util import fmt_ts
    return f"{fmt_ts(ch['start'])}-{fmt_ts(ch['end'])}"


def preferences(st: dict) -> dict:
    if not st.get("use_preferences"):
        return {}
    return {"category_weights": st.get("pref_category_weights") or {}, "duration": st.get("pref_duration")}


def merge_windows(ws: list[tuple[float, float]], pad: float = 2.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ws):
        a, b = max(0.0, a - pad), b + pad
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in out]


def dedupe_events(events: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in sorted(events, key=lambda e: e["t"]):
        dup = False
        for p in reversed(out[-12:]):
            if e["t"] - p["t"] > 9:
                break
            same = p.get("kind") == e.get("kind") and p.get("src") == e.get("src") and (
                p.get("sig") == e.get("sig") if "sig" in e else p.get("amount") == e.get("amount"))
            window = 9 if e.get("src") == "killfeed" else 4
            if same and e["t"] - p["t"] <= window:
                dup = True
                break
        if not dup:
            out.append(e)
    return out


def merge_visual(visual: dict | None, detail: dict) -> dict:
    base = {k: list((visual or {}).get(k, [])) for k in ("t", "motion", "bright", "cut", "face", "gp")}
    for k in base:
        base[k] += detail.get(k, [])
    return base


def save_candidates(db, source_id: int, cands: list[dict], transcript: dict, st: dict) -> list[int]:
    """Replace undecided candidates; keep accepted/rejected/exported ones and skip new ones that repeat them."""
    kept = db.all("SELECT * FROM candidates WHERE source_id=? AND status!='new'", (source_id,))
    db.execute("DELETE FROM candidates WHERE source_id=? AND status='new'", (source_id,))
    seg_info = {s["id"]: {k: s.get(k) for k in ("status", "alt_text", "prev_text", "lang")} for s in transcript["segments"]}
    ids = []
    now = time.time()
    for c in cands:
        if any(_iou(c, k) > 0.5 for k in kept):
            continue
        a, b = c["start"] - CAPTION_MARGIN, c["end"] + CAPTION_MARGIN
        words = [w for w in transcript["words"] if a <= w["start"] <= b]
        segs = {w["seg"] for w in words}
        cap = {"words": words, "segs": {s: seg_info[s] for s in segs if s in seg_info}, "window": [a, b]}
        hooks = hook_options(c["evidence"], sentences(transcript["segments"], c["start"], c["end"]))
        cid = db.insert("candidates", source_id=source_id, start=c["start"], end=c["end"], peak=c["peak"],
                        category=c["category"], score=c["score"], rank_score=c["rank_score"], reason=c["reason"],
                        evidence_json=c["evidence"],
                        features_json={"also": c.get("also", []), "quality": c.get("quality"),
                                       "pref_mult": c.get("pref_mult"), "full_clip": bool(c.get("full_clip"))},
                        hooks_json=hooks, captions_json=cap,
                        edit_json={"hook": hooks[0]["text"] if hooks and st.get("hook_on") else "",
                                   "captions_on": bool(st.get("captions_on")), "censor_on": bool(st.get("censor_on"))},
                        status="new", created_at=now, updated_at=now)
        ids.append(cid)
    return ids


def _iou(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def game_name(gid: str) -> str:
    return game(gid)["name"]

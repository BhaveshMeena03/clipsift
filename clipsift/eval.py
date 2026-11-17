"""Check detection against moments you labelled by hand.

    python -m clipsift.eval "D:\\Recordings\\sample.mp4" labels.json --game cs2

labels.json:
    {"tolerance_s": 10,
     "moments": [{"t": "1:02:03", "category": "skill", "note": "ninja defuse"},
                 {"t": 4410, "category": "funny"}],
     "not_highlights": [{"t": "2:10:00", "note": "loud but boring"}]}

Prints and saves a report. One sample cannot prove the detector works on every game;
the report says what it found on this file only."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import Paths
from .db import DB
from .log import setup_logging
from .util import fmt_ts, parse_ts


def load_labels(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tol = float(data.get("tolerance_s", 10))
    def norm(items):
        out = []
        for m in items or []:
            t = m["t"]
            out.append({**m, "t": float(t) if isinstance(t, (int, float)) else parse_ts(str(t))})
        return out
    return {"tolerance_s": tol, "moments": norm(data.get("moments")), "not_highlights": norm(data.get("not_highlights"))}


def evaluate(cands: list[dict], labels: dict) -> dict:
    tol = labels["tolerance_s"]
    ranked = sorted(cands, key=lambda c: -c["rank_score"])
    rank_of = {c["id"]: i + 1 for i, c in enumerate(ranked)}
    def covers(c, t):
        return c["start"] - tol <= t <= c["end"] + tol
    rows, used = [], set()
    for m in labels["moments"]:
        hits = sorted((c for c in ranked if covers(c, m["t"])), key=lambda c: rank_of[c["id"]])
        if hits:
            h = hits[0]
            used.add(h["id"])
            rows.append({"t": m["t"], "label": m.get("category"), "note": m.get("note", ""), "hit": True,
                         "rank": rank_of[h["id"]], "got": h["category"], "category_ok": h["category"] == m.get("category"),
                         "window": [h["start"], h["end"]], "reason": h["reason"]})
        else:
            near = min(ranked, key=lambda c: min(abs(c["start"] - m["t"]), abs(c["end"] - m["t"])), default=None)
            rows.append({"t": m["t"], "label": m.get("category"), "note": m.get("note", ""), "hit": False,
                         "nearest": None if near is None else [near["start"], near["end"], near["category"]]})
    fps = []
    for nh in labels["not_highlights"]:
        bad = [c for c in ranked if covers(c, nh["t"])]
        if bad:
            fps.append({"t": nh["t"], "note": nh.get("note", ""), "rank": rank_of[bad[0]["id"]], "category": bad[0]["category"],
                        "reason": bad[0]["reason"]})
    n = len(labels["moments"]) or 1
    hits = [r for r in rows if r["hit"]]
    return {"moments": rows, "false_positives": fps, "n_candidates": len(cands),
            "recall": len(hits) / n, "recall_top10": sum(1 for r in hits if r["rank"] <= 10) / n,
            "recall_top20": sum(1 for r in hits if r["rank"] <= 20) / n,
            "category_accuracy": (sum(1 for r in hits if r["category_ok"]) / len(hits)) if hits else None,
            "unlabelled_candidates": len([c for c in cands if c["id"] not in used])}


def report_md(res: dict, video: str) -> str:
    lines = [f"# Detection check: {Path(video).name}", "",
             f"Candidates found: {res['n_candidates']}  ",
             f"Labelled moments found: {res['recall']:.0%} (top 10: {res['recall_top10']:.0%}, top 20: {res['recall_top20']:.0%})  ",
             "Category matched on found moments: " + ("n/a" if res["category_accuracy"] is None else f"{res['category_accuracy']:.0%}"),
             "", "| Time | Labelled | Result | Rank | Got | Why |", "|---|---|---|---|---|---|"]
    for r in res["moments"]:
        if r["hit"]:
            lines.append(f"| {fmt_ts(r['t'])} | {r['label']} | found | {r['rank']} | {r['got']}{'' if r['category_ok'] else ' (different)'} | {r['reason']} |")
        else:
            near = r.get("nearest")
            nn = f"nearest {fmt_ts(near[0])}-{fmt_ts(near[1])} ({near[2]})" if near else "no candidates"
            lines.append(f"| {fmt_ts(r['t'])} | {r['label']} | **missed** | - | - | {nn} |")
    lines += ["", "## Marked as not a highlight but picked anyway", ""]
    lines += [f"- {fmt_ts(f['t'])} ({f['note']}): rank {f['rank']}, {f['category']}: {f['reason']}" for f in res["false_positives"]] or ["- none"]
    lines += ["", f"{res['unlabelled_candidates']} other candidates matched no label. Labels are usually partial, so some "
              "of these may be good; review them in the app before calling them false positives.",
              "", "This checks one recording. It does not show that detection works on other games, maps or audio setups."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m clipsift.eval")
    ap.add_argument("video")
    ap.add_argument("labels")
    ap.add_argument("--game", default="generic")
    ap.add_argument("--layout", default=None, help="name of a saved layout (for kill feed regions)")
    ap.add_argument("--home", default=None)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    paths = Paths(Path(args.home) if args.home else None).ensure()
    setup_logging(paths.logs, "eval")
    db = DB(paths.db)
    labels = load_labels(Path(args.labels))
    video = str(Path(args.video).resolve())
    lid = None
    if args.layout:
        row = db.one("SELECT id FROM layouts WHERE name=?", (args.layout,))
        if not row:
            print(f"No layout named {args.layout!r}", file=sys.stderr)
            return 2
        lid = row["id"]
    src = db.one("SELECT * FROM sources WHERE path=?", (video,))
    sid = src["id"] if src else db.insert("sources", kind="local", path=video, title=Path(video).stem, game=args.game,
                                          layout_id=lid, created_at=time.time(), updated_at=time.time())
    db.update("sources", sid, game=args.game, layout_id=lid, options_json={"max_candidates": args.max})
    from .worker import Worker
    jid = db.add_job("process", "heavy", source_id=sid)
    w = Worker(paths)
    job = db.claim_job("heavy", os.getpid())
    if not job or job["id"] != jid:
        print("Another job is queued first; is the app running? Close it and try again.", file=sys.stderr)
        return 2
    t0 = time.time()
    w.run_job(job)
    j = db.job(jid)
    if j["status"] != "done":
        print(f"Processing failed: {j['error']}", file=sys.stderr)
        return 1
    cands = db.all("SELECT * FROM candidates WHERE source_id=?", (sid,))
    res = evaluate(cands, labels)
    res["processing_s"] = round(time.time() - t0, 1)
    md = report_md(res, video)
    out = Path(args.out) if args.out else Path(video).with_suffix(".clipsift-eval.md")
    try:
        out.write_text(md, encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"(could not save report: {e})", file=sys.stderr)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

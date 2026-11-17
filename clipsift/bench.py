"""Measure processing speed on this PC with a piece of a real recording.

    python -m clipsift.bench "D:\\Recordings\\stream.mp4" --minutes 10 --game cs2

Copies the first N minutes (no re-encode) to a temp folder, processes it in a throwaway data
folder, exports one clip, and prints per-stage times plus a straight-line guess for 9 hours.
The guess is an estimate: speech density and on-screen activity change a lot across a stream."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import Paths
from .db import DB
from .log import setup_logging
from .media import ffmpeg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m clipsift.bench")
    ap.add_argument("video")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--game", default="generic")
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    args = ap.parse_args(argv)
    real = Paths()
    tmp = Path(tempfile.mkdtemp(prefix="clipsift_bench_"))
    try:
        home = Paths(tmp / "home").ensure()
        shutil.rmtree(home.models)
        try:
            os.symlink(real.models, home.models, target_is_directory=True)
        except OSError:
            # no symlink rights on Windows without developer mode: use a junction
            subprocess.run(["cmd", "/c", "mklink", "/J", str(home.models), str(real.models)], check=True,
                           capture_output=True)
        db = DB(home.db)
        st = DB(real.db).settings() if real.db.exists() else {}
        keep = {k: v for k, v in st.items() if k in ("asr_device", "asr_model_gpu", "asr_model_cpu", "language_mode",
                                                     "use_audio_tagger", "encoder", "export_quality", "ingame_names")}
        if keep:
            db.set_settings(keep)
        ffmpeg.configure(real.tools)
        setup_logging(home.logs, "bench")
        piece = tmp / ("piece" + Path(args.video).suffix)
        subprocess.run([ffmpeg.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", args.video, "-t",
                        str(args.minutes * 60), "-map", "0", "-c", "copy", str(piece)], check=True)
        sid = db.insert("sources", kind="local", path=str(piece), title="bench", game=args.game,
                        created_at=time.time(), updated_at=time.time())
        from .worker import Worker
        w = Worker(home)
        jid = db.add_job("process", "heavy", source_id=sid)
        t0 = time.time()
        w.run_job(db.claim_job("heavy", os.getpid()))
        job = db.job(jid)
        total = time.time() - t0
        if job["status"] != "done":
            print("Processing failed:", job["error"])
            return 1
        res = job["result"]
        dur = float(res.get("duration") or args.minutes * 60)
        print(f"\nProcessed {dur / 60:.1f} min in {total:.0f} s ({dur / max(total, 1e-6):.1f}x real time)")
        print(f"Speech model: {res['asr']['model']} on {res['asr']['device']} ({res['asr']['compute_type']})")
        for stage, secs in res.get("timings", {}).items():
            print(f"  {stage:16} {secs:8.1f} s")
        scale = 9 * 3600 / dur
        print(f"\nStraight-line estimate for a 9 hour stream: about {total * scale / 3600:.1f} hours (estimate only).")
        c = db.one("SELECT id, start, end FROM candidates WHERE source_id=? ORDER BY rank_score DESC", (sid,))
        if c:
            jid = db.add_job("export", "light", candidate_id=c["id"])
            t1 = time.time()
            w.run_job(db.claim_job("light", os.getpid()))
            ej = db.job(jid)
            if ej["status"] == "done":
                print(f"Export of one {c['end'] - c['start']:.0f} s clip at 1080x1920: {time.time() - t1:.1f} s")
            else:
                print("Export failed:", ej["error"])
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("kept", tmp)


if __name__ == "__main__":
    sys.exit(main())

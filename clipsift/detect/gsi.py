"""Game event feeds (Valve Game State Integration for CS2 and Dota 2).

The game POSTs its state to ClipSift while you play. We store the events with wall-clock
times, then line them up with a recording by its start time plus a measured offset."""
import json
import logging
import time

log = logging.getLogger(__name__)


def events_for_source(db, src: dict, duration: float) -> list[dict]:
    t0 = src.get("recorded_at")
    if not t0:
        return []
    off = float(src.get("event_offset") or 0.0)
    rows = db.all("SELECT * FROM game_events WHERE t_wall BETWEEN ? AND ? ORDER BY t_wall",
                  (t0 + off - 5, t0 + off + duration + 5))
    out = []
    for r in rows:
        t = r["t_wall"] - t0 - off
        if 0 <= t <= duration:
            out.append({"t": round(t, 2), "src": "gsi", "kind": r["kind"], "game": r["game"], **(r.get("data") or {})})
    return out


# ---------- state diffing ----------

class CS2State:
    """Turns successive CS2 GSI payloads into events. Only the local player's own numbers are used."""

    def __init__(self):
        self.prev: dict | None = None

    def update(self, payload: dict, now: float | None = None) -> list[dict]:
        now = now or time.time()
        evs: list[dict] = []
        p = payload.get("player") or {}
        prov = payload.get("provider") or {}
        own = p.get("steamid") and prov.get("steamid") and p.get("steamid") == prov.get("steamid")
        prev = self.prev or {}
        pp = prev.get("player") or {}
        same_player = own and pp.get("steamid") == p.get("steamid")
        st, pst = p.get("state") or {}, pp.get("state") or {}
        if own and same_player:
            rk, prk = st.get("round_kills"), pst.get("round_kills")
            if isinstance(rk, int) and isinstance(prk, int) and rk > prk:
                hs, phs = st.get("round_killhs") or 0, pst.get("round_killhs") or 0
                for k in range(rk - prk):
                    evs.append({"kind": "kill", "data": {"round_kills": prk + k + 1, "headshot": k < (hs - phs)}})
            if (pst.get("health") or 0) > 0 and st.get("health") == 0:
                evs.append({"kind": "death", "data": {}})
            ms, pms = p.get("match_stats") or {}, pp.get("match_stats") or {}
            if isinstance(ms.get("mvps"), int) and isinstance(pms.get("mvps"), int) and ms["mvps"] > pms["mvps"]:
                evs.append({"kind": "mvp", "data": {}})
        rnd, prnd = payload.get("round") or {}, prev.get("round") or {}
        if rnd.get("bomb") != prnd.get("bomb") and rnd.get("bomb") in ("planted", "defused", "exploded"):
            evs.append({"kind": {"planted": "bomb_planted", "defused": "bomb_defused", "exploded": "bomb_exploded"}[rnd["bomb"]],
                        "data": {}})
        if rnd.get("phase") == "over" and prnd.get("phase") != "over":
            team = p.get("team")
            win = rnd.get("win_team")
            evs.append({"kind": "round_end", "data": {"won": bool(team and win and team == win), "win_team": win,
                                                      "kills": st.get("round_kills")}})
        self.prev = payload
        for e in evs:
            e["t_wall"] = now
        return evs


class Dota2State:
    def __init__(self):
        self.prev: dict | None = None

    def update(self, payload: dict, now: float | None = None) -> list[dict]:
        now = now or time.time()
        evs: list[dict] = []
        p, pp = payload.get("player") or {}, (self.prev or {}).get("player") or {}
        for key, kind in (("kills", "kill"), ("deaths", "death"), ("assists", "assist")):
            a, b = p.get(key), pp.get(key)
            if isinstance(a, int) and isinstance(b, int) and a > b:
                for _ in range(a - b):
                    evs.append({"kind": kind, "data": {}})
        ks, pks = p.get("kill_streak"), pp.get("kill_streak")
        if isinstance(ks, int) and isinstance(pks, int) and ks > pks and ks >= 3:
            evs.append({"kind": "kill_streak", "data": {"streak": ks}})
        self.prev = payload
        for e in evs:
            e["t_wall"] = now
        return evs


def store(db, game: str, events: list[dict]) -> None:
    for e in events:
        db.insert("game_events", game=game, t_wall=e["t_wall"], kind=e["kind"], data_json=json.dumps(e.get("data", {})))


# ---------- setup ----------

CFG = {
    "cs2": '''"ClipSift"
{{
 "uri" "http://127.0.0.1:{port}/gsi/cs2"
 "timeout" "5.0"
 "buffer" "0.1"
 "throttle" "0.1"
 "heartbeat" "10.0"
 "auth"
 {{
  "token" "{token}"
 }}
 "data"
 {{
  "provider" "1"
  "map" "1"
  "round" "1"
  "player_id" "1"
  "player_state" "1"
  "player_match_stats" "1"
 }}
}}
''',
    "dota2": '''"ClipSift"
{{
 "uri" "http://127.0.0.1:{port}/gsi/dota2"
 "timeout" "5.0"
 "buffer" "0.1"
 "throttle" "0.1"
 "heartbeat" "10.0"
 "auth"
 {{
  "token" "{token}"
 }}
 "data"
 {{
  "provider" "1"
  "map" "1"
  "player" "1"
  "hero" "1"
 }}
}}
''',
}
GAME_DIRS = {"cs2": ("Counter-Strike Global Offensive", ("game", "csgo", "cfg")),
             "dota2": ("dota 2 beta", ("game", "dota", "cfg", "gamestate_integration"))}


def config_text(game: str, port: int, token: str) -> str:
    return CFG[game].format(port=int(port), token=token)


def steam_libraries() -> list[str]:
    import re
    import sys
    from pathlib import Path
    roots = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
        roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    libs = []
    for r in roots:
        vdf = Path(r) / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        libs.append(r)
    seen, out = set(), []
    for lib in libs:
        key = lib.lower().rstrip("\\/")
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def install_target(game: str) -> dict:
    from pathlib import Path
    folder, sub = GAME_DIRS[game]
    for lib in steam_libraries():
        base = Path(lib) / "steamapps" / "common" / folder
        if base.is_dir():
            target = base.joinpath(*sub) / "gamestate_integration_clipsift.cfg"
            note = "Restart the game after installing." + (
                " Dota 2 also needs -gamestateintegration in its Steam launch options." if game == "dota2" else "")
            return {"path": str(target), "note": note}
    return {"path": None, "hint": f"Could not find {folder} in your Steam libraries. Copy the text into "
                                  f"steamapps/common/{folder}/{'/'.join(sub)}/gamestate_integration_clipsift.cfg"}


# ---------- lining events up with a recording ----------

def estimate_offset(event_times: list[float], signal_times: list[float], search: float = 120.0, step: float = 0.25,
                    tol: float = 1.5) -> dict | None:
    """Shift that best lines game events (seconds from recording start, by wall clock) up with
    what the video shows (kill feed reads or gunfire). None when the match is not convincing."""
    import bisect
    if len(event_times) < 3 or len(signal_times) < 3:
        return None
    sig = sorted(signal_times)
    best = (0, float("inf"), 0.0)  # hits, total residual, offset
    n_steps = int(search / step)
    for i in range(-n_steps, n_steps + 1):
        off = i * step
        hits, resid = 0, 0.0
        for t in event_times:
            x = t + off
            j = bisect.bisect_left(sig, x - tol)
            near = [abs(sig[k] - x) for k in (j, j + 1) if k < len(sig) and abs(sig[k] - x) <= tol]
            if near:
                hits += 1
                resid += min(near)
        # many shifts match within tolerance; the one that lines up most tightly wins
        if hits > best[0] or (hits == best[0] and hits and resid < best[1] - 1e-9):
            best = (hits, resid, off)
    hits, _resid, off = best
    if hits >= 3 and hits >= 0.4 * len(event_times):
        return {"offset": off, "matched": hits, "of": len(event_times)}
    return None

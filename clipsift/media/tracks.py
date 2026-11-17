"""Audio track roles. roles maps track index (as str) to mixed | mic | discord | game | ignore.

With no roles set, track 0 is used for everything (OBS track 1 is normally the full mix)."""

ROLES = ("mixed", "mic", "discord", "game", "ignore")


def _by_role(roles: dict, n_tracks: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {r: [] for r in ROLES}
    for k, v in (roles or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_tracks and v in ROLES:
            out[v].append(i)
    return {r: sorted(v) for r, v in out.items()}


def plan_tracks(roles: dict, n_tracks: int) -> dict:
    if n_tracks <= 0:
        return {"analysis": [], "voice": [], "export": [], "mute": [], "separate_voice": False}
    r = _by_role(roles, n_tracks)
    parts = r["mic"] + r["discord"] + r["game"]
    if not any(r[x] for x in ("mixed", "mic", "discord", "game")):
        return {"analysis": [0], "voice": [0], "export": [0], "mute": [0], "separate_voice": False}
    if r["mixed"]:
        analysis = r["mixed"][:1]
        export = r["mixed"][:1]
    else:
        analysis = sorted(parts)
        export = sorted(parts)
    speech = r["mic"] + r["discord"]
    voice = sorted(speech) if speech else analysis
    separate = bool(speech) and not r["mixed"]
    mute = sorted(speech) if separate else export
    return {"analysis": analysis, "voice": voice, "export": export, "mute": mute, "separate_voice": separate}

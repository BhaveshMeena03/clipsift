"""Per-game detector profiles. Regions are (x, y, w, h) as fractions of the frame and are only
starting points: the layout screen shows them over a real frame so they can be corrected."""

GAMES = {
    "generic": {
        "name": "Generic / other game",
        "weights": {"skill": 1.0, "funny": 1.0, "scare": 1.0, "other": 1.0},
    },
    "cs2": {
        "name": "Counter-Strike 2 / CS:GO",
        "killfeed": (0.70, 0.05, 0.29, 0.25),
        "banner": (0.25, 0.08, 0.50, 0.26),
        "banner_words": {
            "bomb_defused": ["has been defused", "bomb defused", "defused"],
            "bomb_exploded": ["target destroyed", "bomb exploded"],
            "round_win_ct": ["counter-terrorists win", "counter terrorists win"],
            "round_win_t": ["terrorists win"],
            "mvp": ["mvp"],
        },
        "event_feed": "gsi",
        "weights": {"skill": 1.25, "funny": 1.0, "scare": 0.6, "other": 0.9},
    },
    "valorant": {
        "name": "Valorant",
        "killfeed": (0.70, 0.07, 0.29, 0.28),
        "banner": (0.25, 0.10, 0.50, 0.28),
        "banner_words": {
            "ace": ["ace"], "team_ace": ["team ace"], "clutch": ["clutch"], "flawless": ["flawless"],
            "thrifty": ["thrifty"], "round_win": ["round win", "won"], "spike_defused": ["spike defused", "defused"],
        },
        "weights": {"skill": 1.25, "funny": 1.0, "scare": 0.6, "other": 0.9},
    },
    "dota2": {
        "name": "Dota 2",
        "event_feed": "gsi",
        "weights": {"skill": 1.2, "funny": 1.0, "scare": 0.5, "other": 0.9},
    },
    "rocket_league": {
        "name": "Rocket League",
        "banner": (0.20, 0.12, 0.60, 0.40),
        "banner_words": {
            "goal": ["goal"], "save": ["save"], "epic_save": ["epic save"], "demolition": ["demolition", "demolish"],
            "aerial_goal": ["aerial goal"], "hat_trick": ["hat trick"], "overtime": ["overtime"],
            "winner": ["winner", "victory"],
        },
        "weights": {"skill": 1.2, "funny": 1.0, "scare": 0.4, "other": 0.9},
    },
    "wardogs": {
        "name": "Wardogs",
        # positions are guesses until checked on a real frame in the layout screen
        "killfeed": (0.70, 0.05, 0.29, 0.25),
        "reward": (0.33, 0.30, 0.34, 0.30),
        "weights": {"skill": 1.2, "funny": 1.0, "scare": 0.6, "other": 0.9},
    },
    "rust": {
        "name": "Rust",
        "weights": {"skill": 0.9, "funny": 1.2, "scare": 1.2, "other": 1.0},
    },
    "horror": {
        "name": "Horror game",
        "weights": {"skill": 0.4, "funny": 1.1, "scare": 1.5, "other": 1.0},
    },
    "party": {
        "name": "Party / co-op with friends",
        "weights": {"skill": 0.6, "funny": 1.5, "scare": 1.0, "other": 1.0},
    },
}

# Banner events that count as skill evidence, and how strongly.
SKILL_BANNERS = {"ace": 1.0, "team_ace": 0.6, "clutch": 1.0, "flawless": 0.5, "thrifty": 0.4, "bomb_defused": 0.6,
                 "spike_defused": 0.6, "goal": 0.7, "aerial_goal": 0.9, "epic_save": 0.8, "save": 0.5,
                 "demolition": 0.5, "hat_trick": 1.0, "mvp": 0.4}


def game(gid: str) -> dict:
    return GAMES.get(gid) or GAMES["generic"]


def game_list() -> list[dict]:
    return [{"id": k, "name": v["name"], "hud_ocr": bool(v.get("killfeed") or v.get("banner") or v.get("reward")),
             "event_feed": v.get("event_feed")} for k, v in GAMES.items()]

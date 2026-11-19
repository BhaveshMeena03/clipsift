import numpy as np
import pytest

from clipsift.analysis.asr import _HALL_RE, segment_status
from clipsift.analysis.candidates import (
    find_candidates,
    make_window,
    merge_candidates,
    onsets_per_second,
    pick_peaks,
    quiet_windows,
)
from clipsift.detect import gsi, hud
from clipsift.media.tracks import plan_tracks
from clipsift.pipeline.process import dedupe_events
from clipsift.pipeline.verify import decide
from clipsift.text.hooks import hook_options, sentences

# ---------- windows and merging ----------


def test_make_window_bounds():
    s, e = make_window(5.0, "skill", 300.0, [], 15, 60)
    assert s == 0.0 and e >= 15
    s, e = make_window(295.0, "funny", 300.0, [], 15, 60)
    assert e == 300.0 and e - s >= 15
    s, e = make_window(100.0, "skill", 1000.0, [], 15, 60, kill_times=[60.0, 70.0, 140.0])
    assert e - s <= 60 + 1e-6


def test_make_window_snaps_to_sentence():
    speech = [[85.0, 91.0], [104.0, 112.0]]
    s, e = make_window(100.0, "other", 500.0, speech, 10, 60)  # lead 10 -> 90 is mid-sentence
    assert s <= 85.0 and e >= 106.0
    assert e >= 112.0 - 1e-6


def _c(s, e, cat="skill", score=0.5):
    return {"start": s, "end": e, "category": cat, "score": score, "evidence": [], "peak": (s + e) / 2}


def test_merge_overlapping_and_adjacent():
    out = merge_candidates([_c(10, 30, "funny", 0.4), _c(20, 40, "skill", 0.7), _c(41, 55, "scare", 0.3)], 60)
    assert len(out) == 1
    m = out[0]
    assert (m["start"], m["end"], m["category"]) == (10, 55, "skill")
    assert set(m["also"]) == {"funny", "scare"}


def test_merge_respects_max_len():
    out = merge_candidates([_c(0, 50, score=0.2), _c(20, 75, score=0.9)], 60)
    assert len(out) == 1 and out[0]["score"] == 0.9 and out[0]["start"] == 20
    out = merge_candidates([_c(0, 40, score=0.5), _c(25, 58, score=0.6)], 60)
    assert len(out) == 1 and out[0]["end"] - out[0]["start"] <= 60
    out = merge_candidates([_c(0, 50, score=0.2), _c(40, 95, score=0.9)], 60)
    assert len(out) == 2  # small overlap, too long to join: two different moments


def test_merge_keeps_separate():
    out = merge_candidates([_c(0, 20), _c(100, 120)], 60)
    assert len(out) == 2


def test_pick_peaks_nms_and_floor():
    s = np.zeros(1000, np.float32)
    s[100], s[110], s[500] = 0.9, 0.8, 0.5
    assert pick_peaks(s, 10, floor=0.12, nms=25) == [100, 500]
    assert pick_peaks(s * 0.1, 10, floor=0.12) == []


def test_onsets_detect_bursts():
    flux = np.full(600, 0.5, np.float32) + np.random.default_rng(1).normal(0, 0.02, 600).astype(np.float32)
    flux[[100, 103, 300]] = 5.0
    on = onsets_per_second(flux, 60)
    assert on[10] == 2 and on[30] == 1 and on.sum() == 3


def _audio(n_sec, speech=None):
    n10 = n_sec * 10
    return {"rms_all": np.full(n10, -40.0), "rms_voice": np.full(n10, -45.0), "flux": np.full(n10, 0.5),
            "tags": {}, "has_tags": False, "speech": speech or []}


def test_kill_events_beat_loud_noise():
    n = 1200
    audio = _audio(n)
    audio["rms_voice"][6000:6050] = -5.0  # very loud shout at 600 s, nothing else
    events = [{"t": t, "src": "killfeed", "kind": "kill", "sig": f"k{t}"} for t in (300.0, 303.0, 307.0)]
    cands = find_candidates(n, audio, {"segments": [], "words": []}, None, events, game_id="valorant",
                            max_candidates=10, min_len=15, max_len=60)
    top = cands[0]
    assert top["category"] == "skill"
    assert top["start"] <= 300 - 5 and top["end"] >= 307
    assert any(e["kind"] == "kills" and e["tier"] == "observed" for e in top["evidence"])


def test_funny_speech_is_categorized_funny():
    n = 600
    audio = _audio(n, speech=[[200.0, 206.0]])
    seg = {"id": "s0", "start": 200.0, "end": 206.0, "status": "ok",
           "text": "bro what are you doing, hahaha, bhai kya kar raha hai pagal",
           "words": [{"start": 200.0 + i * 0.5, "end": 200.4 + i * 0.5, "text": " w", "prob": 0.9} for i in range(12)]}
    cands = find_candidates(n, audio, {"segments": [seg], "words": seg["words"]}, None, [], game_id="party",
                            max_candidates=5, min_len=15, max_len=60)
    assert cands and cands[0]["category"] == "funny"
    assert cands[0]["start"] <= 200 <= cands[0]["end"]


@pytest.mark.parametrize("line", ["To bhai, ball par right side par", "Haan bhai.", "Aage aana padega yaar"])
def test_hinglish_filler_words_are_not_funny(line):
    # found on three real Hinglish streams: "bhai" and "yaar" made most callouts look like jokes
    from clipsift.text.lexicon import cue_hits
    assert "funny" not in cue_hits(line)


def test_dropped_segments_do_not_count():
    n = 600
    seg = {"id": "s0", "start": 100.0, "end": 103.0, "status": "dropped", "text": "hahaha lol bro what", "words": []}
    cands = find_candidates(n, _audio(n), {"segments": [seg], "words": []}, None, [], game_id="generic",
                            max_candidates=5, min_len=15, max_len=60)
    assert not any(c["start"] <= 101 <= c["end"] and c["category"] == "funny" for c in cands)


def test_short_vertical_gets_full_clip_candidate():
    cands = find_candidates(120.0, _audio(120), {"segments": [], "words": []}, None, [], game_id="generic",
                            max_candidates=5, min_len=15, max_len=60, orientation="vertical")
    full = [c for c in cands if c.get("full_clip")]
    assert len(full) == 1 and full[0]["start"] == 0 and full[0]["end"] == 120.0


def test_quiet_windows_avoid_speech_and_seeds():
    n = 7200
    # first hour is constant talking, second hour is quiet with sharp sounds
    audio = _audio(n, speech=[[t, t + 55] for t in range(0, 3600, 60)])
    audio["flux"][::37] = 4.0
    ws = quiet_windows(n, audio, None, per_hour=4, avoid=[(4000, 4400)])
    assert len(ws) >= 3
    for a, b in ws:
        assert b - a <= 30.01
        assert a >= 3600 - 30
        assert not (a < 4400 and b > 4000)


# ---------- HUD parsing ----------

def _items(rows):
    out = []
    for r, texts in enumerate(rows):
        x = 10
        for t in texts:
            out.append({"text": t, "x0": x, "x1": x + 100, "y0": r * 30, "y1": r * 30 + 20})
            x += 150
    return out


def test_killfeed_kill_death_and_ocr_noise():
    names = ["fortysevenfrags"]
    rows = hud.killfeed_rows(_items([["f0rtysevenfrags", "Raze_main"], ["enemy", "fortysevenfrag"], ["enemyA", "enemyB"]]), names)
    assert [r["kind"] for r in rows] == ["kill", "death", "feed"]


def test_kill_row_survives_ocr_flicker():
    # found on Windows: the third row read "sova123", then "soval23" a few frames later, and counted as a 4th kill
    names = ["fortysevenfrags"]
    ft = hud.FeedTracker(ttl=9)
    first = hud.killfeed_rows(_items([["fortysevenfrags", "sova123"]]), names)
    later = hud.killfeed_rows(_items([["fortysevenfrags", "soval23"]]), names)
    assert len(ft.update(126, first)) == 1
    assert ft.update(131, later) == []
    events = [{**first[0], "t": 126.0, "src": "killfeed"}, {**later[0], "t": 131.0, "src": "killfeed"}]
    assert len(dedupe_events(events)) == 1


@pytest.mark.parametrize("a,b", [("Me VEPSN IMG Tinbuster", "Me VBPSNIMG Tinbuster"),
                                 ("Me VBPSNIMGTinbuster", "Me VBPSN MGTinbuster"),
                                 ("Me PRG|LEVELUPy", "Me PRGILEVELUP"), ("Me PRG|LEVELUP", "Me PRG|LEVELUP'Y")])
def test_one_kill_read_two_ways_counts_once(a, b):
    # found on a real Valorant stream: 10 of 72 kills were the same row read with "|" as I, space or nothing
    events = [{"t": 100.0, "src": "killfeed", "kind": "kill", "sig": "x", "text": a},
              {"t": 101.5, "src": "killfeed", "kind": "kill", "sig": "y", "text": b}]
    assert len(dedupe_events(events)) == 1


def test_cs2_kill_read_six_ways_counts_once_and_real_victims_stay_apart():
    # found on a real CS2 stream: "12 kills in 13 s" was 3 kills, each read 4 to 6 times through a see-through feed
    reads = ["Forty ira dxhan", "Forty fro dahnn", "Forty iro dahen", "Forty fro dzhen",
             "Forty UlLUstOsAYUSH", "Forty [OullDustatAYUSH", "Forty [DallLusters]AYUSH", "Forty [Ball-Busters]AYUSH",
             "Forty [Ball-Bustersl AYuSH", "Forty [Bal-Busters]AYUSH", "Forty Shaji", "Forty Shoji"]
    events = [{"t": 2742.0 + i, "src": "killfeed", "kind": "kill", "sig": str(i), "text": r} for i, r in enumerate(reads)]
    # 3 real kills -> 4: "ira dxhan" shares too little with any read of "fro dzhen" to be tied to it safely
    assert [e["text"] for e in dedupe_events(events)] == ["Forty ira dxhan", "Forty fro dahnn",
                                                            "Forty UlLUstOsAYUSH", "Forty Shaji"]
    lobby = ["Forty fro dzhen", "Forty [Ball-Busters]AYUSH", "Forty Shoji", "Forty [BINGO69] Kazuto", "Forty C.C."]
    events = [{"t": 10.0 + i, "src": "killfeed", "kind": "kill", "sig": str(i), "text": r} for i, r in enumerate(lobby)]
    assert len(dedupe_events(events)) == 5  # an ace in 5 s stays 5 kills


def test_wardogs_kill_read_without_its_victim_counts_once():
    # found on a real Wardogs stream: the feed prints a distance before the victim and some reads catch only
    # that ("Forty- [37m]", "Forty 157ml"); victims with Chinese names folded to nothing and never matched
    reads = [(1327, "Forty [37m][LS]FL4vours"), (1330, "Forty- [37m]"), (1335, "Forty 157ml"),
             (1341, "Forty [8m]lmxLolo"), (2062, "Forty [34m]爱笑的草丛"), (2064, "Forty [34m]爱笑的草从"),
             (2066, "Forty [13m]残星会"), (5000, "Forty [55m]"), (5003, "Forty 55m")]
    events = [{"t": float(t), "src": "killfeed", "kind": "kill", "sig": str(t), "text": r} for t, r in reads]
    # a read with no victim near a readable one is that kill; on its own it is still a kill, counted once
    assert [e["text"] for e in dedupe_events(events)] == ["Forty [37m][LS]FL4vours", "Forty [8m]lmxLolo",
                                                            "Forty [34m]爱笑的草丛", "Forty [13m]残星会", "Forty [55m]"]
    assert hud.victimless("Forty 2mT") and hud.victimless("Forty- [37m]") and not hud.victimless("Forty C.C.")


def _kills(reads):
    return [{"t": t, "src": "killfeed", "kind": "kill", "sig": str(i), "text": r} for i, (t, r) in enumerate(reads)]


def test_wardogs_distance_ties_garbled_reads_of_one_kill():
    # found on a real Wardogs stream: "13 kills in 13 s" was 4 kills; a Chinese name read 9 ways kept its "[20m]"
    reads = [(6672.5, "Forty [34m]1TTTT]entence"), (6672.5, "Forty [33m][毛长]Achon10"), (6674.0, "Forty [34m][TTTT]音"),
             (6676.0, "Forty [20m][年子情大]镇北看速运院已唐克."), (6677.5, "FortyV [20m]王统大]纽北速信脱疤"),
             (6681.5, "FortyP [20m]生子征大】北飞柜兆"), (6685.5, "Forty [20m][牛子掘大]纽北急速脱施克0）")]
    assert [e["text"] for e in dedupe_events(_kills(reads))] == [
        "Forty [34m]1TTTT]entence", "Forty [33m][毛长]Achon10", "Forty [20m][年子情大]镇北看速运院已唐克."]
    # "7 kills in 8 s" was 2: "[8m]" also read as "18m]", "[7m]" as "(7m)" and "17mm"
    reads = [(13239.0, "Fortyr [8m]向心而行采你位"), (13240.0, "Forty 18m]两心m行壹桑均信"), (13240.0, "Forty 17mm"),
             (13240.5, "Forty (7m) MHTOTI"), (13241.0, "FortyD 18m]同m行细付"), (13243.5, "FortyV [7m]-咖啡"),
             (13247.0, "Forty [8m]向心而行後果你付 Xeruosst 99001")]
    assert [e["text"] for e in dedupe_events(_kills(reads))] == ["Fortyr [8m]向心而行采你位", "Forty (7m) MHTOTI"]
    # two enemies at the same distance with readable, different names stay two kills
    assert len(dedupe_events(_kills([(10.0, "Forty [20m]Raider"), (12.0, "Forty [20m]Chalice")]))) == 2
    assert hud.same_distance("62", "82") and hud.same_distance("18", "8") and not hud.same_distance("34", "33")


def test_short_name_read_three_ways_counts_once():
    # found on a real CS2 stream: one kill on "Mac" read as "Mac", "Mic" and "MBC" was "3 kills in 2 s"
    assert len(dedupe_events(_kills([(5586.0, "Forty Mac"), (5586.5, "Forty Mic"), (5587.5, "Forty MBC")]))) == 1
    assert len(dedupe_events(_kills([(10.0, "Forty Mac"), (11.0, "Forty Sam")]))) == 2


def test_cs2_mvp_counts_only_when_it_names_you():
    # found on a real CS2 stream: 25 MVP reads, most of them a teammate's or, after a lost round, an enemy's
    mine = [{"text": "ROUND WON"}, {"text": "Forty threw3grenades thatround."}, {"text": "MVP"}]
    other = [{"text": "ROUND WON"}, {"text": "dlgglr killed 2enemies with headshots that round."}, {"text": "MVP"},
             {"text": "digglr"}]
    assert [e["kind"] for e in hud.banner_events(mine, "cs2", ["Forty"])] == ["mvp"]
    assert hud.banner_events(other, "cs2", ["Forty"]) == []
    assert hud.banner_events(mine, "cs2") == []  # no in-game names set: no MVP is known to be yours


def test_different_victims_are_different_kills():
    names = ["fortysevenfrags"]
    a = hud.killfeed_rows(_items([["fortysevenfrags", "player1"]]), names)
    b = hud.killfeed_rows(_items([["fortysevenfrags", "player2"]]), names)
    ft = hud.FeedTracker(ttl=9)
    assert len(ft.update(10, a)) == 1 and len(ft.update(11, b)) == 1


def test_valorant_shows_your_own_name_as_me():
    # found on a real Valorant stream: your kills read "Me [gun] Reyna", your deaths "TheKryne [gun] Me"
    from clipsift.detect.games import game
    names = ["fortysevenfrags", *game("valorant")["self_names"]]
    rows = hud.killfeed_rows(_items([["Me", "Reyna"], ["TheKryne", "Me"], ["Gekko", "Leon S Kennedy"]]), names)
    assert [r["kind"] for r in rows] == ["kill", "death", "feed"]
    assert not hud.name_match("Me", ["fortysevenfrags"])  # other games: "Me" is not you
    assert not hud.name_match("Mo", names) and not hud.name_match("Mel", names)


def test_wardogs_distance_tags_and_self_kills():
    # found on a real Wardogs stream: the victim side reads "[41m] Forty", and a grenade on yourself
    # shows your name on both sides
    rows = hud.killfeed_rows(_items([["MaMy_PoKo", "[41m] Forty"], ["Forty", "[1m] Forty"],
                                     ["Forty", "[12m] Raider"], ["[TH]HarukiXD", "[15m] Chalice"]]), ["Forty"])
    assert [r["kind"] for r in rows] == ["death", "death", "kill", "feed"]
    assert hud.name_match("[TH] Forty", ["Forty"]) and not hud.name_match("[Forty]", ["Fortnite"])


def test_name_match_rejects_short_and_different():
    assert not hud.name_match("ab", ["fortysevenfrags"])
    assert not hud.name_match("fortunate", ["fortysevenfrags"])


@pytest.mark.parametrize("text,want", [("ACE", "ace"), ("TEAM ACE", "team_ace"), ("CLUTCH", "clutch"),
                                       ("SPACE", None), ("FLAWLESS", "flawless")])
def test_banner_words(text, want):
    ev = hud.banner_events([{"text": text}], "valorant")
    kinds = [e["kind"] for e in ev]
    if want is None:
        assert "ace" not in kinds
    else:
        assert want in kinds
        if want == "team_ace":
            assert "ace" not in kinds


def test_rocket_league_banners():
    kinds = {e["kind"] for e in hud.banner_events([{"text": "EPIC SAVE!"}], "rocket_league")}
    assert kinds == {"epic_save"}


def test_trackers_count_once():
    ft = hud.FeedTracker(ttl=9)
    row = [{"kind": "kill", "sig": "a|b", "text": "a b"}]
    assert len(ft.update(10, row)) == 1
    assert ft.update(12, row) == []
    assert ft.update(15, row) == []
    assert len(ft.update(30, row)) == 1
    bt = hud.BannerTracker(ttl=6)
    assert len(bt.update(1, [{"kind": "ace"}])) == 1 and bt.update(3, [{"kind": "ace"}]) == []


@pytest.mark.parametrize("text,amounts", [("+$300", [300]), ("+S250", [250]), ("$ 1,200", [1200]), ("12:34", []),
                                          ("round 3", []), ("+150 +$75", [150, 75])])
def test_reward_amounts(text, amounts):
    assert hud.reward_amounts([{"text": text}]) == amounts


# ---------- tracks ----------

def test_plan_tracks_defaults_and_roles():
    assert plan_tracks({}, 3)["analysis"] == [0]
    p = plan_tracks({"0": "mixed", "1": "mic", "2": "game"}, 3)
    assert p["export"] == [0] and p["voice"] == [1] and p["mute"] == [0] and not p["separate_voice"]
    p = plan_tracks({"0": "ignore", "1": "mic", "2": "discord", "3": "game"}, 4)
    assert p["export"] == [1, 2, 3] and p["voice"] == [1, 2] and p["mute"] == [1, 2] and p["separate_voice"]
    assert plan_tracks({"7": "mic"}, 2)["analysis"] == [0]
    assert plan_tracks({}, 0)["export"] == []


# ---------- game state feed ----------

def _cs(kills, hs=0, health=100, bomb=None, phase="live", steam="1", prov="1", mvps=0, team="CT", win=None):
    return {"provider": {"steamid": prov}, "player": {"steamid": steam, "team": team,
            "state": {"round_kills": kills, "round_killhs": hs, "health": health}, "match_stats": {"mvps": mvps}},
            "round": {"phase": phase, "bomb": bomb, "win_team": win}}


def test_cs2_state_events():
    s = gsi.CS2State()
    assert s.update(_cs(0), now=1.0) == []
    ev = s.update(_cs(2, hs=1), now=2.0)
    assert [e["kind"] for e in ev] == ["kill", "kill"]
    assert ev[0]["data"]["headshot"] and not ev[1]["data"]["headshot"]
    ev = s.update(_cs(2, bomb="defused", phase="over", win="CT", mvps=1), now=3.0)
    assert {e["kind"] for e in ev} == {"bomb_defused", "round_end", "mvp"}
    assert next(e for e in ev if e["kind"] == "round_end")["data"]["won"]
    ev = s.update(_cs(0, health=0, phase="live"), now=4.0)
    assert [e["kind"] for e in ev] == ["death"]


def test_cs2_ignores_spectated_players():
    s = gsi.CS2State()
    s.update(_cs(0, steam="2", prov="1"), now=1.0)
    assert s.update(_cs(3, steam="2", prov="1"), now=2.0) == []


def test_dota_events():
    s = gsi.Dota2State()
    s.update({"player": {"kills": 1, "deaths": 0, "kill_streak": 1}})
    ev = s.update({"player": {"kills": 3, "deaths": 0, "kill_streak": 3}})
    assert [e["kind"] for e in ev] == ["kill", "kill", "kill_streak"]


# ---------- ASR and verification rules ----------

@pytest.mark.parametrize("text", ["Thanks for watching!", "Please subscribe to my channel", "nan", "Subtitles by Amara.org"])
def test_hallucination_phrases(text):
    assert _HALL_RE.search(text)


def test_segment_status_rules():
    assert segment_status({"flags": ["hallucination_phrase"]}) == "dropped"
    assert segment_status({"flags": [], "words": []}, speech_overlap=0.1) == "dropped"
    assert segment_status({"flags": ["low_logprob"], "avg_logprob": -0.9, "words": []}) == "flagged"
    words = [{"prob": 0.2}, {"prob": 0.3}, {"prob": 0.9}]
    assert segment_status({"flags": [], "words": words}, 0.9) == "flagged"
    assert segment_status({"flags": [], "words": [{"prob": 0.9}]}, 0.9) == "ok"


def test_verify_decide():
    seg = {"text": "bhai kya kar raha hai", "words": [{"prob": 0.5}] * 5}
    same = [{"text": " bhai kya kar raha hai", "prob": 0.6}]
    assert decide(seg, same)["status"] == "verified"
    better = [{"text": " bhai kaha ja raha hai", "prob": 0.9}]
    assert decide(seg, better)["status"] == "revised"
    weak = {"text": "xx yy", "words": [{"prob": 0.3}]}
    assert decide(weak, [{"text": " zz", "prob": 0.35}])["status"] == "unclear"
    assert decide(weak, [])["status"] == "unclear"
    strong = {"text": "hello there", "words": [{"prob": 0.8}]}
    assert decide(strong, [{"text": " yellow chair", "prob": 0.75}])["status"] == "disputed"


# ---------- hooks ----------

def test_hooks_are_grounded():
    ev = [{"kind": "kills", "count": 3, "span": 6.4, "label": "3 kills in 6 s (kill feed OCR)"},
          {"kind": "banner", "banner": "clutch", "label": "clutch"}]
    segs = [{"start": 10, "end": 14, "status": "ok", "words": [
        {"start": 10, "end": 10.3, "text": " Bhai", "prob": 0.9}, {"start": 10.4, "end": 10.6, "text": " kya", "prob": 0.9},
        {"start": 10.7, "end": 11, "text": " kar", "prob": 0.9}, {"start": 11, "end": 11.3, "text": " raha", "prob": 0.9},
        {"start": 11.3, "end": 11.6, "text": " hai?", "prob": 0.9}]},
            {"start": 20, "end": 22, "status": "flagged", "words": [{"start": 20, "end": 21, "text": " pagal hai kya", "prob": 0.9}]}]
    opts = hook_options(ev, sentences(segs, 0, 30))
    texts = [o["text"] for o in opts]
    assert texts[:2] == ["3 kills in 6 seconds", "Clutch"]
    # two kill rows read in one frame (found on a real Wardogs stream, shown as "2 kills in 0 s")
    assert hook_options([{"kind": "kills", "count": 2, "span": 0.0, "label": "x"}], [])[0]["text"] == "2 kills at once"
    assert '"Bhai kya kar raha hai?"' in texts
    assert all("pagal" not in t for t in texts)  # flagged line is not quoted


def test_no_hook_without_grounding():
    assert hook_options([], []) == []


def test_tiny_low_confidence_bits_are_dropped():
    assert segment_status({"start": 599.33, "end": 599.47, "avg_logprob": -1.1, "flags": ["low_logprob"],
                           "words": [{"prob": 0.2}]}, 1.0) == "dropped"
    assert segment_status({"start": 10.0, "end": 10.3, "avg_logprob": -0.2, "flags": [],
                           "words": [{"prob": 0.95}]}, 1.0) == "ok"   # a clear short "yes!" stays


def test_second_listen_ignores_stretched_or_outside_words():
    from clipsift.pipeline.verify import alt_words_for
    seg = {"start": 599.33, "end": 599.47}
    alt = [{"flags": [], "words": [{"text": "Haan", "start": 599.49, "end": 602.07},      # 2.6 s word: a guess
                                   {"text": "is", "start": 599.34, "end": 599.46},        # inside: kept
                                   {"text": "later", "start": 601.0, "end": 601.3}]}]     # outside: not this line
    assert [w["text"] for w in alt_words_for(seg, alt)] == ["is"]

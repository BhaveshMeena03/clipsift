# Testing

## Test suite

```
data\tools\uv\uv.exe pip install --python .venv\Scripts\python.exe --require-hashes -r setup\requirements-dev-windows.txt
set PYTHONPATH=%CD%
.venv\Scripts\python -m pytest
```

Unit tests (fast, no models): timestamps, chunk planning and stitching (every word kept exactly once across boundaries), caption line building (no overlaps, synced to word starts, edits survive trims), Roman Hindi conversion, profanity masking and mute spans, glossary merging, candidate windows and duplicate merging, HUD parsing (kill vs death, OCR noise, banner words), game event diffing and clock alignment, render graphs (crop/fit/full/vertical/insets, muting only voice tracks), job queue (exclusive claims, crash limit), checkpoint invalidation, API security and validation, CSV metrics parsing, preference maths (missing is not zero, age adjustment), YouTube link and error handling, download job with a fake yt-dlp.

Media tests (real ffmpeg, PyAV, OCR, speech and audio models; found automatically in the app's `data` folder, or set `CLIPSIFT_TEST_MODELS`): audio/frame timestamp accuracy, the labelled synthetic sample, resume skips finished work, cancel then resume gives identical candidates, caption pixels appear exactly while their line plays, swears masked in captions and silent in the exported audio while the rest of the reaction stays, source file bytes unchanged, vertical sources not reframed, preview caching, disk-full refusal, corrupt and silent files, a 22 minute recording whose worker is killed with SIGKILL mid-transcription and resumed, cancelling during video decoding (this once deadlocked inside PyAV; the test hung 1 run in 3 before the fix and 0 in 16 after), reference caption-style reading.

Last run on macOS (M3 Pro): 266 tests passed in about 3 minutes, including simulated RTX 40, GTX 10, multi-GPU, AMD, Intel and no-GPU setups. The speech tests make voices with macOS or Windows text-to-speech and skip where neither works.

## Checking detection on your own footage (Milestone 2)

1. Pick a recording (30 to 60 minutes is enough) that has at least one quiet skill play and one funny spoken moment.
2. Write `labels.json` next to it:

```json
{
  "tolerance_s": 10,
  "moments": [
    {"t": "0:12:40", "category": "skill", "note": "ninja defuse, no talking"},
    {"t": "0:31:05", "category": "funny", "note": "squad argues about the plant"}
  ],
  "not_highlights": [
    {"t": "0:20:00", "note": "loud but nothing happens"}
  ]
}
```

3. Run:

```
.venv\Scripts\python -m clipsift.eval "D:\Recordings\sample.mp4" labels.json --game cs2 --layout "My layout"
```

It processes the file like the app does and writes `sample.clipsift-eval.md` and `.json`: which labelled moments were found, at what rank and category, which were missed (with the nearest candidate), and which "not highlights" were picked anyway. The candidates also appear in the app's Review screen.

## Result on the synthetic sample

The synthetic sample is a 4 minute 1080p video with a fake facecam, a fake Valorant-style kill feed showing three kills by `fortysevenfrags` and a CLUTCH banner with no talking or gunfire (the quiet skill play), English and Hindi TTS lines with laughing words (the funny moment), and a loud "testing the microphone" line (the decoy).

| Moment | Found | Rank | Category |
|---|---|---|---|
| Quiet skill play at 2:08 | yes | 1 of 3 | skill: "3 kills in 6 s (kill feed OCR); clutch (on-screen text, OCR)" |
| Funny spoken moment at 0:45 | yes | 2 of 3 | funny: quotes of what was said, loudness change |
| Loud decoy at 3:15 | picked anyway | 3 of 3 | other |

This proves the pipeline mechanics (OCR on a HUD, speech, timing, ranking), not real gameplay understanding: the fake HUD is clean text, the voices are synthetic, there is no game audio. Treat real-footage quality as unverified until you run the step above.

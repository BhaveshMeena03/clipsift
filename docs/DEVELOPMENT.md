# Working on ClipSift

Start here when picking the project up. It covers where things stand, how to run and test it on Windows, the rules the code follows, and what to do next.

## What it is

A local Windows app for the fortysevenfrags channel (CS2, Valorant, Dota 2, Rocket League, Wardogs, Rust, horror and party games; English, Hindi and Hinglish speech). It finds promising moments in long stream recordings and exports 9:16 clips for Shorts and Reels. No paid services, no cloud processing. The original requirements are summarised in the README and the docs folder; [LIMITATIONS.md](LIMITATIONS.md) is the honest list of gaps.

## State (19 Nov 2026)

| Area | State |
|---|---|
| Milestones 1 to 5 (pipeline, eval harness, YouTube + long streams + resume, review/edit/export UI, references) | Built. 266 tests pass on macOS (Apple M3 Pro, CPU only); 318 pass on Windows (RTX 4070 Ti) |
| Windows install, CUDA speech, NVENC export | **Verified on Windows** (RTX 4070 Ti PC, 19 Nov 2026): `Install.bat` finished, hinglish-apex loads and transcribes on the GPU in float16, NVENC passes its test encode. A friend's RTX 4060 PC is next |
| AMD AMF and Intel QSV export | Written and unit-tested with simulated hardware only; not run on a real AMD or Intel PC |
| Detection quality on real streams | **Unverified.** Only a synthetic labelled sample was checked |
| HUD regions (kill feed, banners, money popups) | Default positions are guesses, Wardogs especially; must be set on a real frame |

## Run it (Windows)

```bat
cd /d D:\ClipSift
Install.bat
ClipSift.bat
```

Data (database, models, tools, downloads, exports, logs) lives in `D:\ClipSift\data` unless setup was given `-DataDir`. Logs: `data\logs\server.log` and `data\logs\worker.log`, or Storage & logs in the app.

Command-line tools need the app folder on `PYTHONPATH`:

```bat
set PYTHONPATH=%CD%
.venv\Scripts\python -m clipsift.models status
.venv\Scripts\python -m clipsift.bench "D:\path\stream.mp4" --minutes 10 --game cs2
.venv\Scripts\python -m clipsift.eval "D:\path\stream.mp4" labels.json --game cs2 --layout "My layout"
```

## Test and lint (Windows)

One-time dev tools (hash-locked, same versions as the app's packages):

```bat
data\tools\uv\uv.exe pip install --python .venv\Scripts\python.exe --require-hashes -r setup\requirements-dev-windows.txt
```

Then:

```bat
set PYTHONPATH=%CD%
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest
```

The tests find ffmpeg and the models in `data\` by themselves. Speech tests make voices with the Windows speech engine; if it is missing they skip. Media tests use CPU speech (hinglish-swift) on purpose so results do not depend on the GPU. Expect a few minutes for the full run.

## Rules the code follows

- **Production grade.** Every real failure found becomes a test. Lint the whole repo and run the full test suite before every push; check the push landed.
- **Commits:** one short plain subject line, lowercase, no body, no trailers.
- **Docs and UI text:** no em or en dashes. Plain words over jargon. Code comments short.
- **Honesty:** scores are ranking estimates, never virality predictions. Evidence is labelled observed / model / inferred. Captions never fill in words the model did not hear. Say "unverified" when something is.
- **Never** overwrite or modify source recordings; never write exports over existing files.
- **No paid APIs, no accounts, no logins.** YouTube: public videos only, no cookies. Instagram: CSV, official API with the user's own token, or best-effort public reads.
- Pin every dependency and checksum every download.

## Map of the code

| Path | What |
|---|---|
| `clipsift/app.py` | launcher: port, worker supervisor (restart, stall watchdog), app window |
| `clipsift/server/` | FastAPI API (local only, cookie + Host check), references/GSI routes |
| `clipsift/web/` | the UI (plain HTML/JS/CSS, no build step) |
| `clipsift/worker.py` | job lanes heavy / light / download, keep-awake, pause on app exit |
| `clipsift/pipeline/process.py` | the per-recording pipeline and its checkpoints |
| `clipsift/analysis/` | speech (asr), audio scan, video scan, candidate scoring |
| `clipsift/detect/` | game profiles, HUD OCR parsing, CS2/Dota game state feed |
| `clipsift/text/` | Roman Hindi, captions, glossary, profanity, hooks |
| `clipsift/render/` | 9:16 ffmpeg graph, caption/hook images, encoder choice |
| `clipsift/prefs/` | reference library, preference suggestions, Instagram |
| `setup/` | Windows installer, model conversion, hash-locked requirements |
| `tests/` | unit, media and long-stream tests; `synth.py` builds the labelled synthetic stream |

## Things learned the hard way (do not undo)

- **PyAV log level stays `None`** (`media/frames.py`). Any numeric level installs a Python log callback that deadlocked frame-threaded decoding when a job was cancelled mid-video (hung 1 run in 3; 0 in 16 after the fix).
- **CPU speech threads stay at CTranslate2's default (4).** Using all 12 cores of an M3 Pro was 4x slower; Intel 12th gen+ CPUs mix fast and slow cores the same way.
- **Whisper can return one segment across minutes of silence.** `resegment()` splits on word gaps; speech checks are per word.
- **Tiny low-confidence segments at the end of speech are noise** ("Is.", "Haan haan."); they are dropped, and the second caption listen ignores words centred outside the line or stretched over silence.
- **Stock Whisper garbles mixed Hindi/English.** The Oriserve Hinglish models (run with `language=en`) are the defaults. Stock models get a code-switch repair pass.
- **Proxies are named by time span and previews by render settings**, because SQLite reuses row ids after re-processing.
- **uvicorn re-raises SIGTERM after shutdown**, so the worker is stopped from the FastAPI lifespan hook, with per-PID stop files. The worker also pauses itself if the app process disappears.
- **Windows keep-awake (`SetThreadExecutionState`) is per thread;** each lane sets and clears its own.
- **Hardware encoders are chosen by a real test encode** with the export options; a failure on a real clip retires that encoder and the render retries down to x264.
- **Speech precision comes from what the card supports** (GTX 10-series: int8). With several NVIDIA cards, the one with the most free memory is used (`CUDA_DEVICE_ORDER=PCI_BUS_ID`).
- **yt-dlp needs Deno + yt-dlp-ejs** for YouTube; setup installs Deno.
- **RapidOCR must not upscale small crops** (`det_limit_type=max`): same reads, about 2x faster.
- **On Windows, `.venv\Scripts\python.exe` is a launcher** that starts the real python.exe as its child, so `Popen.pid` of anything started with `sys.executable` is the launcher. Use `interpreter_pid()` for stop files and job ownership and `kill_tree()` to kill. Before this, closing the app never reached the worker's stop file and the stall watchdog never matched a job.
- **Kill feed rows are compared with OCR lookalikes folded** (`1 l i`, `0 o`, `5 s`, `8 b`, `2 z`). Windows OCR read `sova123` as `soval23` on one frame and counted a fourth kill.
- **Never paste ffmpeg output into a filter graph.** ffmpeg also prints the input file's tags, which anyone can fill. Loudness values are read only after the last `[Parsed_loudnorm` line and only as finite numbers.

## Next steps, in order

1. Done (19 Nov 2026): `Install.bat` finished on the RTX 4070 Ti PC with `Speech will run on: NVIDIA GeForce RTX 4070 Ti ...` and `Video encoder: NVIDIA NVENC`, and hinglish-apex, hinglish-swift and ced-mini ready. Nothing failed.
2. Done (19 Nov 2026): ruff clean and all 318 tests pass on Windows. The first run found three Windows-only bugs (venv launcher PIDs, a test using SIGKILL, OCR flicker double-counting kills) and a security review found nine more issues; all are fixed with tests.
3. First real run in the app on a short recording (10 to 30 min): Settings (in-game names), Layout (draw facecam, gameplay and kill feed boxes on a real frame), Process, Review, Export. Check the exported clip plays with sound and captions.
4. `clipsift.bench` on 10 minutes of a real stream with normal talking. Put the measured numbers into PERFORMANCE.md.
5. Label one recording with a quiet skill play and a funny moment and run `clipsift.eval` (format in TESTING.md). Report misses and false positives honestly; tune scoring only with evidence from these reports.
6. Replace the guessed HUD default regions (Wardogs first) with ones measured on real frames.
7. Set up the CS2 game event feed (Settings > Live game events) before a stream, then process that stream and check the events line up.
8. Install on the friend's RTX 4060 PC.

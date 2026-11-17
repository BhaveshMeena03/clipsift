# ClipSift architecture

ClipSift finds promising moments in long gaming recordings and turns the ones you pick into 9:16 clips. Everything runs on your PC. Nothing is uploaded.

## Stack and why

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.11 | All the local ML tools (faster-whisper, ONNX Runtime, PyAV) ship Windows wheels for it |
| Speech to text | faster-whisper (CTranslate2), MIT | Runs Whisper on the RTX 4060 in float16, falls back to CPU int8, gives word timestamps and word probabilities |
| Audio events | sherpa-onnx + CED AudioSet tagger, Apache-2.0 | Laughter, screaming, gunshots and music as model outputs, CPU only, about 50 MB |
| On-screen text | RapidOCR (ONNX), Apache-2.0 | Reads kill feeds and round banners for CS2 and Valorant with no extra installer |
| Video decode for analysis | PyAV | Keyframe-only and sampled decode with exact timestamps, no temp frames on disk |
| Render | ffmpeg CLI | Crop, stack, mix, mute, loudness and encode, with NVENC when present |
| Captions and hooks | Pillow images on an ffmpeg overlay | Works with any ffmpeg build (no libass needed), same fonts on every PC |
| YouTube | yt-dlp + Deno, public videos only | No cookies, no login, clear errors for private, members-only, live and region-locked |
| UI | FastAPI on 127.0.0.1 + plain HTML/JS, opened as an Edge app window | Video preview, range requests and the layout editor are simple in a browser engine |
| Storage | SQLite (WAL) + JSON/NPZ files per source | Survives restarts; every stage writes atomically and is skipped on resume |

## Processes

```
launcher (ClipSift.bat)
  └─ server process: FastAPI, UI, API, starts and watches the worker
       └─ worker process: runs jobs from the SQLite queue
            lanes: heavy (processing, one at a time) | light (previews, exports) | download
```

The worker is a separate process so a GPU crash cannot take the UI down. The server restarts it, and a job that crashes the worker three times is marked failed instead of looping. A watchdog also restarts the worker if a running job makes no progress for 15 minutes (a native hang in a decoder or driver), and the job resumes from its checkpoints. If the app is closed or killed, the worker notices its parent is gone and pauses its jobs. While a job runs on Windows, the worker keeps the PC from sleeping.

## Processing pipeline (per source)

1. **probe**: duration, size, orientation, audio tracks, keyframe spacing.
2. **audio_scan** (cheap, whole file, streamed in 10 minute blocks): loudness, sharp transients, voice activity (Silero VAD), audio event tags.
3. **transcribe**: 10 minute chunks with 20 s overlap, cut at silence, timestamps kept absolute. One JSON per chunk, so a restart skips finished chunks.
4. **visual_scan** (whole file, keyframes or 1 fps): motion, scene cuts, flashes; for CS2/Valorant also kill feed and banner OCR. Saved per 30 minute block.
5. **candidates**: per-second category scores (skill, funny, scare, other), peak picking, lead-in and aftermath, snap to sentence edges, merge overlaps, quotas per category.
6. **visual_detail** (expensive, only on candidate windows plus sampled quiet windows): 2 fps facecam motion and HUD OCR. Quiet windows exist so a silent ninja defuse can still surface.
7. **verify_captions**: re-decode unclear lines with surrounding audio, compare passes, flag or hold back lines that do not agree.
8. **proxies**: small horizontal preview per candidate with a margin for trimming.

Every piece of evidence carries a tier:

- **observed**: measured directly (loudness jump, OCR read the text "ACE").
- **model**: a model output with its own probability (speech text, "laughter p=0.71").
- **inferred**: an interpretation built on the above ("few kills before the defuse, maybe a ninja defuse"). Shown as a guess.

## Output

1080x1920 H.264/AAC MP4. Horizontal sources use a saved layout profile (facecam on top, gameplay in the middle, branding panel at the bottom) with crop, fit or full-frame gameplay framing and optional HUD insets. Vertical sources keep their composition and only get captions and hook text.

## Data folders (`%LOCALAPPDATA%\ClipSift`)

```
clipsift.db        jobs, sources, candidates, decisions, layouts, references, settings
work\<source>\     stage outputs (safe to delete, re-created on demand)
previews\          low-res vertical previews
downloads\         YouTube recordings (kept until you delete them)
exports\           final clips (default, can be changed)
models\            whisper and tagger models
logs\              rotating logs
tools\             ffmpeg and deno installed by the setup script
```

Source recordings are opened read-only, are never written to, and are never deleted by cleanup.

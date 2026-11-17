# Performance

## Measured (this build, macOS, Apple M3 Pro, CPU only, no CUDA)

| Test | Result |
|---|---|
| 4 min synthetic 1080p30 stream, full processing (Valorant OCR on, 1 fps video scan, Hinglish Swift on CPU) | 24 s (10x real time). Audio scan 1.2 s, transcription 1.7 s (little speech), video scan 7.6 s, closer look 3.7 s, caption check 6.2 s, previews 3.0 s |
| 22 min low-res recording, killed mid-run and resumed | 32 s total including restart; finished chunks not redone |
| Speech model speed on 36 s of continuous speech, CPU int8 | hinglish-swift 3.6x real time, hinglish-apex 1.7x real time |
| Export, 22 s clip, 1080x1920, x264 `slow` CRF 16 | 11 s |
| Export, 22 s clip, earlier x264 `medium` CRF 18 | 5.6 s |

The synthetic stream has very little speech, so its transcription time says nothing about a real stream. Speech time dominates on real streams.

## Not measured: RTX 4060 + Ryzen 5 3600

No Windows/NVIDIA machine was available. Rough expectations, **estimates only**:

- Speech (hinglish-apex, float16 on the 4060): tens of times faster than real time. For a 9 hour stream with 4 to 5 hours of speech, roughly 10 to 30 minutes.
- Video scan: depends on keyframe spacing. With 2 s keyframes it decodes about 16k frames for 9 hours (minutes). With sparse keyframes it decodes everything at 1 fps sampling, which on a Ryzen 5 3600 is roughly 1 to 2 hours for 1080p60. Kill feed OCR adds time only when the feed region changes.
- Whole 9 hour stream overnight: likely 1 to 3 hours on GPU, much longer on CPU only.
- Export with NVENC: a few seconds to tens of seconds per clip.

## Measure it on your PC

```
.venv\Scripts\python -m clipsift.bench "D:\Recordings\some stream.mp4" --minutes 10 --game cs2
```

Copies the first 10 minutes, processes them in a throwaway folder with your settings and models, exports one clip, and prints per-stage times and a straight-line estimate for 9 hours. Use a stretch with normal talking so the speech model is exercised.

## Memory and disk

- Audio is streamed in 10 minute blocks and video frames are decoded one at a time; a 9 hour recording is never loaded into memory.
- Peak RAM is mostly the speech model (about 2 to 4 GB) plus OCR; one heavy model at a time.
- Work files per 9 hour stream: tens of MB of analysis data plus small preview clips (a few MB each).
- Disk space is checked before downloads (room for parts plus the merged file) and before each export.

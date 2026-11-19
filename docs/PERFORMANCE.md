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

## Measured: Windows 11, RTX 4070 Ti, Core i7-13700K, 32 GB (Nov 2026)

Three real 1080p60 YouTube streams with Hinglish talk and kill feed OCR on. Their keyframes are far apart, so the video scan decoded everything at 1 fps.

| Stream | Total | Audio scan | Speech | Video scan | Closer look | Caption check | Previews |
|---|---|---|---|---|---|---|---|
| Valorant, 1h34m | 57 min | 32 s | 1 min | 39 min | 14.5 min | 1.4 min | 1.1 min |
| CS2, 1h44m | 60 min | 39 s | 1.4 min | 40 min | 15.4 min | 1.7 min | 1.1 min |
| Wardogs, 3h50m | 74 min | 1.5 min | 4.3 min | 50 min | 13.7 min | 2.3 min | 1.2 min |

- The video scan is most of the time, and not only for decoding: at the same resolution and frame rate it cost about 24 minutes per hour of video for Valorant and CS2 but 13 for Wardogs. The difference is kill feed OCR, which runs whenever the feed region changes.
- Speech on the GPU costs about 1 minute per hour of stream.
- The closer look costs about 14 minutes whatever the length, since it covers a capped number of stretches.
- A 9 hour stream: about 2.5 hours for a game like Wardogs, about 4 hours for Valorant or CS2.
- Processing a stream again reuses every scan, unless the in-game names or the layout boxes changed: then the video scan and the closer look run again in full.
- The `hwaccel_decode` setting is not used yet: the video scan decodes on the CPU.

## Not measured: RTX 4060 PC

Speech will be a little slower than on the 4070 Ti. The video scan depends mostly on that PC's CPU, since decoding and OCR run there.

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

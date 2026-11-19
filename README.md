# ClipSift

Finds promising moments in long gaming recordings and turns the ones you pick into 9:16 clips for YouTube Shorts and Instagram Reels. Built for fortysevenfrags: CS2, Valorant, Dota 2, Rocket League, Wardogs, Rust, horror and party games, English + Hindi + Hinglish speech.

Everything runs on your PC. No accounts, no subscriptions, no cloud processing. After setup, local files process fully offline.

## What it does

1. **Add** a finished YouTube stream/VOD link (public or unlisted) or local MP4s: full horizontal streams, short horizontal clips, or existing vertical clips.
2. **Layout**: draw your facecam and gameplay boxes over a real frame once, save it as a profile. Facecam on top, gameplay in the middle, branding panel at the bottom.
3. **Process** (overnight is fine): cheap whole-recording scans first (audio levels, gunfire-like transients, speech, laughter/screams, motion, kill feed), then speech-to-text, then a closer look at candidate stretches and at quiet stretches so a silent ninja defuse can still surface. Resumes after a restart.
4. **Review** candidates grouped as skill plays, funny squad moments, reactions/scares and other. Each has a preview, timestamp (a link for YouTube sources), reason, evidence labelled as observed / model / inferred, and caption warnings.
5. **Edit** quickly: trim, hook text, caption fixes, censor on/off, crop/fit/full framing, facecam size. Render a low-res vertical preview.
6. **Export** selected clips at 1080x1920 in one batch. Recordings are never modified.

While you play CS2 or Dota 2 with ClipSift open, the games' official event feed (Valve Game State Integration, the same thing SteelSeries Moments uses) records your kills, defuses and round results, and processing lines them up with the recording.

## Start here

- Windows install: [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md)
- How to use it: [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- Models, sizes, licenses: [docs/MODELS.md](docs/MODELS.md)
- When it uses the internet: [docs/NETWORK.md](docs/NETWORK.md)
- What it cannot do (read this): [docs/LIMITATIONS.md](docs/LIMITATIONS.md)
- Speed: [docs/PERFORMANCE.md](docs/PERFORMANCE.md)
- Tests and checking detection on your own footage: [docs/TESTING.md](docs/TESTING.md)
- How it is built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Picking up development (state, rules, next steps): [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

## Status, honestly

| Area | State |
|---|---|
| Pipeline, captions, export, resume, cancel | Working, covered by unit tests and end-to-end media tests on macOS and Windows |
| Windows install, CUDA speech, NVENC export | **Verified on Windows** on an RTX 4070 Ti PC: install, GPU speech, NVENC, full test suite |
| AMD AMF and Intel Quick Sync export | Written and tested with simulated hardware; not yet run on a real AMD or Intel PC |
| Detection quality on real gameplay | **Unverified.** Only checked on a synthetic labelled sample. Use `python -m clipsift.eval` on your own labelled footage |
| CS2/Valorant/Wardogs HUD regions | Default positions are guesses; correct them on the Layout screen |
| Instagram | CSV export and the official API are supported; public scraping is best effort and often blocked |

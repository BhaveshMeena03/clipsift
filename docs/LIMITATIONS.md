# Known limitations

Read this before trusting the picks.

## Detection

- **Detection quality on your real streams is unverified.** It was only checked on a synthetic labelled sample (fake HUD, TTS voices). Run `python -m clipsift.eval` on a recording you label yourself; see TESTING.md.
- **No model understands gameplay.** Skill plays are found from evidence that can be read or measured: kill feed text with your name on the killer side, round banners (ACE, CLUTCH, defused), money popups, CS2/Dota game event feeds, gunfire-like audio and callouts. A great flick that produces no feed entry, banner or reaction can be missed. Quiet stretches get a closer look to reduce this, but not every quiet minute is checked.
- **"Your kill" from OCR is positional.** A row counts as your kill when your in-game name is the left-most text. Games or settings that lay the feed out differently will be misread. Set your in-game names in Settings.
- **Default HUD regions are guesses** for every game, Wardogs especially. Draw them over a real frame on the Layout screen.
- **Clutch and ninja defuse** are only claimed when a banner or the game feed says so. ClipSift cannot count alive enemies, so it does not label something a clutch on its own.
- **Rust, horror and party games** rely on speech, laughter/scream detection, loudness after quiet, facecam movement and scene changes. Scares without a reaction are hard to find.
- **Laughter and scream detection** comes from a general AudioSet model. Game sounds can trigger it; quiet laughter can be missed.
- **Loud does not mean good**: loudness is one weak signal among many, but a long shouting stretch can still rank above a quiet good moment. The "other" category catches these.
- **Valorant has no official event feed** for players; kill feed OCR is the only game-side evidence.
- **CS2/Dota event alignment** uses the recording's start time (OBS file name, file metadata or YouTube's stream start) and is checked against kill feed reads. Without kill feed reads, it is clock-only and could be off by a few seconds.

## Captions

- Speech models can mishear names, slang and crosstalk. The caption check re-reads unclear lines and flags or hides them, but a confident wrong word can still pass. Review flagged lines before exporting.
- Word timing is about ±0.2 s. Muted swears use a little padding for this.
- **Profanity detection is a word list**, not a classifier. Misheard swears, new slang and Hindi spelled unusually can slip through. Words you edit in get estimated mute times.
- Roman Hindi spelling varies (`accha`/`acha`); output follows the model or the converter's rules. Use the glossary for names.
- Overlapping speech from several friends is transcribed as one stream; speakers are not separated.

## Video and audio

- Separate OBS tracks help a lot. With one mixed track, muting a swear also mutes game sound for that fraction of a second.
- HDR sources are tone-mapped only by what ffmpeg does by default; "Best" download prefers SDR.
- Variable frame rate recordings are converted to constant frame rate on export.

## Platform

- **The Windows scripts and CUDA/NVENC paths have not been run on Windows yet.** Everything else was tested on macOS (Apple M3 Pro) with the same code.
- YouTube changes often. yt-dlp may need updating (`.venv\Scripts\python -m pip install -U yt-dlp yt-dlp-ejs`); downloads that YouTube blocks are reported, not worked around.
- Instagram usually hides post numbers from logged-out visitors. CSV export or the official API (needs a professional account and your own token) are the reliable routes.

## Preferences

- Views and likes depend on timing, the algorithm and luck. Suggestions are patterns with sample counts, never causes. With fewer than about 8 clips with metrics, treat them as weak hints.

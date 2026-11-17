# User guide

## 1. Add recordings

- **YouTube**: paste the link, press Check. You see title, length, the quality that will be downloaded and its size. "Best available" takes the highest resolution YouTube has (often 1440p or 4K for streams), preferring H.264 only when it is the same resolution. Private, members-only, age-restricted, region-locked, still-live and still-processing videos are refused with a clear message. Downloads resume after a restart and start processing when they finish.
- **Local files**: Choose video files, or paste full paths. Multiple files are fine. OBS file names like `2026-11-17 20-15-03.mp4` are read as the recording start time (used to line up CS2/Dota events).
- **Game**: set it per recording. It picks which detectors run (kill feed, banners, money popups) and how categories are weighted.
- **Audio tracks**: if OBS recorded separate tracks, mark which is mic, Discord, game or a full mix. Speech is read from mic + Discord; swear muting then hits only those tracks so game sound stays.

## 2. Layout

Pick a horizontal recording, scrub to a frame that shows your HUD, then drag the boxes:

- **facecam** (blue) and **gameplay** (green) become the top and middle panels.
- **killfeed / banner / reward** (orange/yellow) are where the game shows kills, round banners and money popups. They are read with OCR. Defaults are rough guesses per game; move them onto the real HUD. Each game keeps its own regions.
- **Gameplay mode**: Crop (fills the panel, loses the sides), Fit (whole gameplay box, blurred fill), Full frame (nothing cropped).
- **Insets**: small HUD pieces copied onto every clip. Separately, when a clip's proof (say the kill feed) would be cropped out, it is added as an inset automatically; you can switch that off per clip.
- Panel heights, branding text and colours. The 9:16 sketch on the right updates live; it is a mock, the real render is the preview in Review.

Vertical recordings ignore layouts: they keep their own composition and only get captions and hook text.

## 3. Process

Options apply to the next run: language (Hinglish default), how many candidates, clip length range, how many quiet stretches per hour get a closer look, video scan mode. Long recordings are processed in chunks with checkpoints. You can cancel, close the app or lose power; Resume continues from the last finished chunk. Re-processing keeps clips you already accepted, rejected or exported.

What runs, in order: audio scan, speech-to-text, video scan, candidate picking, closer look at candidates and quiet stretches, caption check, previews.

## 4. Review

Tabs filter by category and status. Each card shows the source time (click for YouTube), length, why it was picked and evidence tiers:

- **observed**: measured or read from the screen (loudness jump, OCR read "CLUTCH", game feed said kill)
- **model**: a model output that can be wrong (speech text, "laughter p=0.72")
- **inferred**: an interpretation built on the above

The rank number orders moments inside one recording. It is not a prediction of views.

Accept and Reject are saved and feed the preference suggestions.

## 5. Edit and preview

- **Trim**: type a time or nudge by 1 s / 5 s, or set to the playhead of the source preview.
- **Hook**: pick one of the suggestions (always taken from counted events or something actually said), write your own, or none.
- **Captions**: edit any line. Orange lines have low-confidence words or a second reading that disagreed; red lines were unclear twice and are hidden until you check them (✓ or edit). The "other reading" shows what the second pass heard. Captions never fill in words the model did not hear.
- **Light censor**: masks swears in captions (`f**k`) and mutes just those words. If you typed a swear into a line, its mute time is estimated; check the preview.
- **Framing**: crop / fit / full, pan, facecam size or off, automatic HUD insets.
- **Render vertical preview** gives a 540x960 preview with everything applied.

## 6. Export

Tick accepted clips and Export. Each becomes a 1080x1920 H.264 MP4 (NVENC when available, otherwise x264, max quality settings), loudness-normalised to about -14 LUFS, named `fortysevenfrags_<recording>_<time>_<category>.mp4`. Existing files are never overwritten; the source is never touched. Every export is checked after rendering (size, length, audio) before it is kept.

## Glossary

Settings > Glossary, one per line: `Correct term = how it gets heard, another way`. Example: `Raze = raise, rays`. Applied to new processing runs (no re-transcription needed for the replacement itself).

## CS2 / Dota 2 live events

Settings > Live game events > Set up CS2 writes a small config file into the game's `cfg` folder. Restart the game. While ClipSift is open, kills, headshots, deaths, bomb plants/defuses, MVPs and round results are stored with timestamps. When you later process a recording from that session, ClipSift places the events using the recording's start time and checks them against kill feed reads; it tells you if it could only use clock time. Dota 2 also needs `-gamestateintegration` in its Steam launch options. Valorant has no official event feed for players, so it uses kill feed OCR.

## References (optional)

Import your past clips, add views/likes by hand or from a CSV (Meta Business Suite > Insights > Content > Export), or the official Instagram API with your own token. Mark favourites and rejects. Analyse reads each clip's length, moment type, speech pace, cut rate and burned-in caption style. Suggestions show what your better clips have in common, with how many clips they are based on. Apply the ones you agree with; turn preferences off any time. Blank metrics mean unknown, not zero.

## Storage

Storage & logs shows space used. Clearing previews or work files never touches recordings, downloads or exports; work files come back if you re-process. Removing a YouTube recording asks separately whether to delete its downloaded file.

# When ClipSift uses the internet

## During setup only

| What | From |
|---|---|
| uv, Python 3.11.18 | github.com/astral-sh, python-build-standalone (via uv) |
| Python packages | pypi.org (hash-checked) |
| ffmpeg 9.0.2 | github.com/GyanD/codexffmpeg |
| Deno 2.9.8 | github.com/denoland/deno |
| Speech and audio models | huggingface.co (Oriserve), github.com/k2-fsa/sherpa-onnx |

## While running, only when you ask for it

| Action | Talks to |
|---|---|
| Check / download a YouTube link | youtube.com, googlevideo.com (yt-dlp, no account, no cookies) |
| Download an optional model from Settings | huggingface.co or github.com |
| Fetch from Instagram API | graph.facebook.com, with the token you entered (stored only in your local database, hidden in the UI and logs) |
| Read public Instagram post numbers | instagram.com via yt-dlp, no login |

## Never

- Processing local files, transcription, detection, previews and exports never use the network.
- No telemetry, no accounts, no uploads, no automatic posting.
- The app listens on 127.0.0.1 only. Other devices on your network cannot reach it, and other websites in your browser cannot drive it (Host check, custom header on changes). Only the window opened from the ClipSift shortcut gets API access: it carries a one-time launch code signed with this install's secret, so other programs and other Windows accounts on the PC see the page but cannot use it.
- The CS2/Dota event feed is a local POST from the game to 127.0.0.1 with its own token.

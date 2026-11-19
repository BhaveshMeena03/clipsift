# Installing on Windows

Built for Windows 10/11 64-bit PCs like an RTX 4070 Ti or RTX 4060 with 16 GB RAM. The install has been verified on an RTX 4070 Ti PC (GPU speech and NVENC); AMD and Intel PCs have not been tried yet. If a step fails, the log on screen says which one.

## What runs where

ClipSift checks the PC when it starts a job and uses the best option that actually works, so the same install works on different PCs.

| Hardware | Speech to text | Export encoder |
|---|---|---|
| NVIDIA RTX 20/30/40 series (4070 Ti, 4060, ...) | GPU, float16 (int8 if the card is busy) | NVENC, max quality |
| NVIDIA GTX 10/16 series | GPU, int8 (these cards have no fast float16) | NVENC (plain settings if the driver rejects the max ones) |
| NVIDIA RTX 50 series | GPU if the CUDA libraries support it, otherwise CPU (says which) | NVENC |
| AMD Radeon | CPU (slower; no AMD support in the speech runtime on Windows) | AMD AMF |
| Intel Arc / Intel graphics | CPU | Intel Quick Sync |
| No GPU | CPU | x264 on the CPU |

Every hardware encoder is proven with a short test encode before use, and if one still fails on a real clip the export retries with the next one, ending with x264. With two NVIDIA cards, speech runs on the one with the most free memory. The installer prints what it found at the end ("Speech will run on: ...", "Video encoder: ...").

## Needs

- 64-bit Windows 10 or 11
- An up-to-date graphics driver. NVIDIA cards need a 2023 or newer driver for GPU speech. Without a usable NVIDIA GPU, speech runs on the CPU, which is much slower.
- About 8 GB free for the app and models, plus room for downloads and exports. A 9 hour 1080p60 VOD is roughly 10 to 30 GB; 4K is more.
- Internet during setup.

## Install

1. Put the `clipsift` folder on any drive with room, for example `D:\ClipSift` (avoid OneDrive-synced folders and Program Files). A GitHub ZIP unzips to `clipsift-main`; rename or move it. Everything ClipSift stores goes inside this folder.
2. Double-click **`Install.bat`**. It runs `setup\install.ps1`, which:
   - downloads uv, Python 3.11.18, ffmpeg 9.0.2 and Deno 2.9.8, checking each against a pinned SHA-256;
   - installs Python packages from `setup\requirements-windows.txt` (every package pinned and hash-checked), including NVIDIA's cuBLAS/cuDNN libraries for the speech model;
   - downloads the audio event model and converts the two Hinglish speech models (`setup\convert_models.ps1`, which uses a temporary PyTorch environment and deletes it afterwards);
   - puts a **ClipSift** shortcut on the desktop.
3. First install takes a while (about 2 GB of packages and 2 GB of models). Running `Install.bat` again skips finished steps.

Options: `setup\install.ps1 -DataDir E:\ClipSiftData` keeps data and models somewhere other than the app folder (remembered in `data_dir.txt`). `-SkipModels` skips step 3's models.

## Start

Double-click the **ClipSift** shortcut (or `ClipSift.bat`). A small console window starts and the app opens in an Edge app window at `http://127.0.0.1:8765`.

- Keep the console window open while processing. Closing it pauses jobs; they resume next start.
- ClipSift keeps the PC awake while it is working (the screen can still turn off).
- Only one copy runs at a time. Starting it again opens the existing window.

## Data folder

`data\` inside the app folder (for example `D:\ClipSift\data`) holds the database, work files, previews, downloads, exports (default), models, tools and logs. Nothing goes to the C: drive except the desktop shortcut. Settings > Export folder moves exports anywhere.

## Updating

Copy the new files over the app folder (keep `.venv` and `data`), or `git pull`, then run `Install.bat` again to pick up any new pinned packages.

## Uninstalling

Delete the app folder (this deletes its `data` folder with downloads and default exports too; move exports first) and the desktop shortcut. If you set up CS2/Dota event feeds, delete `gamestate_integration_clipsift.cfg` from the game's cfg folder.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Settings shows "CUDA devices for speech: 0" | Update the NVIDIA driver. ClipSift still works on CPU, slower. The log names the missing DLL if cuBLAS/cuDNN failed to load. |
| Settings shows "Video encoder: x264 on CPU" on a gaming PC | Update the graphics driver. Exports still work on the CPU, just slower. |
| YouTube check says "needs Deno" | Run `Install.bat` again, or set the Deno path in Settings. |
| YouTube says "bot check" or rate limit | Try later. ClipSift does not sign in or work around it. |
| "ffmpeg missing" in the top bar | Run `Install.bat` again or set ffmpeg's path in Settings. |
| Port 8765 busy | Another program uses it. ClipSift picks another port, but CS2/Dota event feeds expect 8765. |
| Something else | Storage & logs shows the worker and server logs. Secrets are removed from logs. |

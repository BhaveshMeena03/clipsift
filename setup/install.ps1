<#
ClipSift setup for Windows 10/11 (64-bit).

Downloads (all pinned, each checked against a SHA-256 before use):
  uv 0.13.8                      astral-sh/uv GitHub release          ~20 MB
  Python 3.11.18                 python-build-standalone, via uv      ~30 MB
  Python packages                PyPI, hash-locked (requirements-windows.txt)  ~1.6 GB incl. CUDA libraries
  ffmpeg 9.0.2 essentials        GyanD/codexffmpeg GitHub release     ~110 MB
  Deno 2.9.8                     denoland/deno GitHub release          ~45 MB (YouTube needs it)
  Models                         see docs/MODELS.md                    ~2 GB

Run again any time: finished steps are skipped.
#>
param(
  [string]$DataDir = "",
  [switch]$SkipModels,
  [switch]$NoShortcut
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar on

$Root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = $Root
# Data (models, downloads, exports, tools) lives in data\ next to the app, on whatever drive
# it was unzipped to. -DataDir picks another folder and is remembered in data_dir.txt.
$DataFile = Join-Path $Root "data_dir.txt"
if ($DataDir) {
  $DataDir = [System.IO.Path]::GetFullPath($DataDir)
  [System.IO.File]::WriteAllText($DataFile, $DataDir)
} elseif (Test-Path $DataFile) {
  $DataDir = [System.IO.File]::ReadAllText($DataFile).Trim()
} else {
  $DataDir = Join-Path $Root "data"
}
Write-Host "ClipSift data folder: $DataDir"
$Tools = Join-Path $DataDir "tools"
$Cache = Join-Path $Tools "_downloads"
New-Item -ItemType Directory -Force -Path $Tools, $Cache | Out-Null

# Only this Windows account, SYSTEM and Administrators may read or change ClipSift's code, tools and data.
# Folders on drives other than C: let every signed-in account modify them by default, which would let
# another account swap ffmpeg.exe or the Python files for its own, or read the app's secrets.
function Protect-Folder([string]$Path) {
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls $Path /inheritance:r /grant:r "*${me}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
  if ($LASTEXITCODE) { Write-Warning "Could not limit who can change $Path (icacls exit code $LASTEXITCODE)." }
}
Protect-Folder $Root
if (-not ([System.IO.Path]::GetFullPath($DataDir)).StartsWith(([System.IO.Path]::GetFullPath($Root)).TrimEnd('\') + '\')) {
  Protect-Folder $DataDir
}

function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }

function Get-Verified([string]$Url, [string]$Sha256, [string]$OutFile) {
  if ((Test-Path $OutFile) -and ((Get-FileHash $OutFile -Algorithm SHA256).Hash -eq $Sha256.ToUpper())) {
    Write-Host "   already downloaded: $(Split-Path -Leaf $OutFile)"
    return
  }
  Write-Host "   downloading $Url"
  $part = "$OutFile.part"
  Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
  $got = (Get-FileHash $part -Algorithm SHA256).Hash
  if ($got -ne $Sha256.ToUpper()) {
    Remove-Item $part -Force
    throw "Checksum mismatch for $Url (got $got). Not using it."
  }
  Move-Item -Force $part $OutFile
}

# Files from a GitHub ZIP carry the "downloaded from the internet" mark, which makes SmartScreen
# block the .bat files. Clear it on ClipSift's own files only.
Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notlike "*\.venv\*" } |
  Unblock-File -ErrorAction SilentlyContinue

$arch = (Get-CimInstance Win32_OperatingSystem).OSArchitecture
if ($arch -notmatch "64") { throw "ClipSift needs 64-bit Windows." }

Step "uv (Python installer)"
$uvZip = Join-Path $Cache "uv-0.13.8.zip"
Get-Verified "https://github.com/astral-sh/uv/releases/download/0.13.8/uv-x86_64-pc-windows-msvc.zip" `
  "529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8" $uvZip
$uvDir = Join-Path $Tools "uv"
if (-not (Test-Path (Join-Path $uvDir "uv.exe"))) { Expand-Archive -Force $uvZip $uvDir }
$uv = Join-Path $uvDir "uv.exe"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR = Join-Path $Tools "uv-cache"

Step "Python 3.11 and packages"
& $uv python install 3.11.18
if ($LASTEXITCODE) { throw "Python install failed" }
$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  & $uv venv --python 3.11.18 $venv
  if ($LASTEXITCODE) { throw "venv creation failed" }
}
$py = Join-Path $venv "Scripts\python.exe"
& $uv pip sync --python $py --require-hashes (Join-Path $PSScriptRoot "requirements-windows.txt")
if ($LASTEXITCODE) { throw "Package install failed" }

Step "ffmpeg"
$ffZip = Join-Path $Cache "ffmpeg-9.0.2-essentials_build.zip"
$ffDir = Join-Path $Tools "ffmpeg"
if (-not (Test-Path (Join-Path $ffDir "bin\ffmpeg.exe"))) {
  Get-Verified "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip" `
    "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba" $ffZip
  $tmp = Join-Path $Tools "_ffmpeg_tmp"
  if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
  Expand-Archive -Force $ffZip $tmp
  $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
  if (Test-Path $ffDir) { Remove-Item -Recurse -Force $ffDir }
  Move-Item $inner.FullName $ffDir
  Remove-Item -Recurse -Force $tmp
} else { Write-Host "   already installed" }

Step "Deno (needed by yt-dlp for YouTube)"
$denoZip = Join-Path $Cache "deno-2.9.8.zip"
$denoDir = Join-Path $Tools "deno"
if (-not (Test-Path (Join-Path $denoDir "deno.exe"))) {
  Get-Verified "https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-pc-windows-msvc.zip" `
    "CBA84B26E0DF5AE7F74FC66B86A95B866872A86292EBD84FA1E959927EDD93BF" $denoZip
  Expand-Archive -Force $denoZip $denoDir
} else { Write-Host "   already installed" }

Step "Checking the install"
& $py -c "import faster_whisper, ctranslate2, av, sherpa_onnx, rapidocr_onnxruntime, yt_dlp; print('packages ok')"
if ($LASTEXITCODE) { throw "Package check failed" }
$env:CLIPSIFT_TOOLS = $Tools
& $py -c "import os; from clipsift.gpu import system_info; from clipsift.analysis.asr import plan_asr; from clipsift.media import ffmpeg; from pathlib import Path; ffmpeg.configure(Path(os.environ['CLIPSIFT_TOOLS'])); i = system_info(); print('GPU:', [g['name'] for g in i['gpus']] or 'none found', '| CUDA devices:', i['cuda_devices']); print('Speech will run on:', plan_asr({}, None).reason); print('Video encoder:', ffmpeg.hw_encoder_label() or 'x264 on CPU')"

if (-not $SkipModels) {
  Step "Models"
  & $py -m clipsift.models --home $DataDir download ced-mini
  if ($LASTEXITCODE) { Write-Warning "Audio event model download failed; you can retry from Settings." }
  & (Join-Path $PSScriptRoot "convert_models.ps1") -DataDir $DataDir
}

if (-not $NoShortcut) {
  $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "ClipSift.lnk"
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = Join-Path $Root "ClipSift.bat"
  $s.WorkingDirectory = $Root
  $s.WindowStyle = 7  # minimized console
  $s.IconLocation = (Join-Path $Root "clipsift\web\clipsift.ico") + ",0"
  $s.Save()
  Write-Host "   desktop shortcut: $lnk"
}

Step "Done"
& $py -m clipsift.models --home $DataDir status
Write-Host ""
Write-Host "Start ClipSift with the desktop shortcut or ClipSift.bat." -ForegroundColor Green

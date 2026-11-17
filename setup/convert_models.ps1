<#
Converts Oriserve's Hinglish Whisper models (Apache-2.0) to the format ClipSift runs.
They are published as transformers checkpoints; conversion is a one-time step that needs
PyTorch, so it runs in a separate temporary environment that is deleted afterwards.

  hinglish-apex   Oriserve/Whisper-Hindi2Hinglish-Apex   ~1.6 GB download, ~1.6 GB on disk (GPU default)
  hinglish-swift  Oriserve/Whisper-Hindi2Hinglish-Swift  ~0.3 GB download, ~0.15 GB on disk (CPU default)

Both are pinned to an exact commit (see clipsift/models.py).
#>
param(
  [string]$DataDir = "$env:LOCALAPPDATA\ClipSift",
  [string[]]$Models = @("hinglish-apex", "hinglish-swift")
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = $Root
$Tools = Join-Path $DataDir "tools"
$uv = Join-Path $Tools "uv\uv.exe"
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $uv) -or -not (Test-Path $py)) { throw "Run install.ps1 first." }
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools "python"
$env:UV_CACHE_DIR = Join-Path $Tools "uv-cache"

$list = & $py -m clipsift.models --home $DataDir convert-list | ConvertFrom-Json
$todo = @()
foreach ($m in $Models) {
  $info = $list.$m
  if (-not $info) { throw "Unknown model $m" }
  if (Test-Path (Join-Path $info.out "model.bin")) { Write-Host "$m already converted"; continue }
  $todo += $m
}
if (-not $todo) { return }

$cv = Join-Path $Tools "convert-venv"
$hf = Join-Path $Tools "hf-temp"
$env:HF_HOME = $hf
$env:HF_HUB_DISABLE_TELEMETRY = "1"
try {
  & $uv venv --python 3.11.18 $cv
  if ($LASTEXITCODE) { throw "convert venv failed" }
  & $uv pip sync --python (Join-Path $cv "Scripts\python.exe") --require-hashes (Join-Path $PSScriptRoot "requirements-convert-windows.txt")
  if ($LASTEXITCODE) { throw "convert packages failed" }
  $conv = Join-Path $cv "Scripts\ct2-transformers-converter.exe"
  foreach ($m in $todo) {
    $info = $list.$m
    $tmp = "$($info.out).tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Write-Host "Converting $m from $($info.repo)@$($info.revision)"
    & $conv --model $info.repo --revision $info.revision --output_dir $tmp `
      --copy_files tokenizer.json preprocessor_config.json --quantization float16
    if ($LASTEXITCODE) { throw "conversion of $m failed" }
    if (Test-Path $info.out) { Remove-Item -Recurse -Force $info.out }
    Move-Item $tmp $info.out
  }
} finally {
  # PyTorch and the original checkpoints are only needed for this step
  if (Test-Path $cv) { Remove-Item -Recurse -Force $cv -ErrorAction SilentlyContinue }
  if (Test-Path $hf) { Remove-Item -Recurse -Force $hf -ErrorAction SilentlyContinue }
}
Write-Host "Models ready."

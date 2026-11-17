@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ClipSift is not installed yet. Double-click Install.bat first.
  pause
  exit /b 1
)
title ClipSift - keep this window open while processing
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m clipsift %*
if errorlevel 1 pause

@echo off
rem Runs setup\install.ps1 without changing the PowerShell execution policy for the system.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1" %*
echo.
pause

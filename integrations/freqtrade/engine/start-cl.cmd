@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-live.ps1" -ConfigPath "config.cl.live.yaml"
pause

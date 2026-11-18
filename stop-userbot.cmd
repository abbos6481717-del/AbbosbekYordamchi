@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-userbot.ps1"
timeout /t 4

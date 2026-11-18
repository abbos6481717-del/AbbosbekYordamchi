@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-userbot.ps1"
timeout /t 6

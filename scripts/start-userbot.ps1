# Starts the userbot in the background (no console window), restarting it if it already runs.
# A hidden loop keeps it alive (see run-userbot-loop.ps1).
# -AtLogon: used by the Windows Startup shortcut; waits 30 s for the internet first.
# Logs: data\userbot.log (main), data\userbot-console.log, data\userbot-supervisor.log.
param([switch]$AtLogon)

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

& (Join-Path $PSScriptRoot 'stop-userbot.ps1') | Out-Null

$delay = if ($AtLogon) { 30 } else { 0 }
Start-Process -FilePath 'powershell.exe' `
    -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', `
        '-File', ('"{0}"' -f (Join-Path $PSScriptRoot 'run-userbot-loop.ps1')), '-StartDelay', $delay `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Write-Host "Bot ishga tushdi (yashirin, oynasiz). To'xtab qolsa, o'zi qayta yonadi."
Write-Host "Holatini bilish: Telegram -> Saqlangan xabarlar -> /status"
Write-Host "To'xtatish: stop-userbot.cmd"

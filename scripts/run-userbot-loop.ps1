# Keeps the userbot running: starts it hidden and restarts it 30 s after it stops
# (lost internet, a crash). Exit code 2 means a configuration or login problem that needs
# a person, so the loop stops then. Started by start-userbot.ps1; stopped by stop-userbot.ps1.
param([int]$StartDelay = 0)

$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$stdin = Join-Path $data 'stdin-empty.txt'
if (-not (Test-Path $stdin)) { New-Item -ItemType File -Path $stdin | Out-Null }
$supervisorLog = Join-Path $data 'userbot-supervisor.log'

function Note([string]$text) {
    Add-Content -Path $supervisorLog -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text)
}

if ($StartDelay -gt 0) {
    Note "waiting $StartDelay s before start (internet after logon)"
    Start-Sleep -Seconds $StartDelay
}

while ($true) {
    Note "starting bot"
    $process = Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
        -ArgumentList 'bot.py', '--userbot' `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardInput $stdin `
        -RedirectStandardOutput (Join-Path $data 'userbot-stdout.log') `
        -RedirectStandardError (Join-Path $data 'userbot-console.log') `
        -PassThru -Wait
    $code = $process.ExitCode
    if ($code -eq 2) {
        Note "bot stopped with code 2 (settings or login problem) - not restarting; see data\userbot.log"
        break
    }
    Note "bot stopped with code $code - restarting in 30 s"
    Start-Sleep -Seconds 30
}

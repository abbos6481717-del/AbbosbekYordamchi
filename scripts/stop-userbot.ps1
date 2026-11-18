# Stops the background userbot and its restart loop.
$stopped = 0
# The loop first, otherwise it would start the bot again.
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'run-userbot-loop\.ps1' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'bot\.py"?\s+--userbot(\s|$)' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }
if ($stopped) { Write-Output "Bot to'xtatildi." } else { Write-Output "Bot ishlamayotgan edi." }

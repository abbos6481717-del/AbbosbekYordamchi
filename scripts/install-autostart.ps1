# Adds the Windows Startup shortcut that starts the bot (hidden) when you log in.
# -Remove deletes it (do this on the OLD computer when you move the bot, so two computers
# never answer for the same Telegram account at once).
param([switch]$Remove)

$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'Telegram yordamchi bot.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $link) {
        Remove-Item -LiteralPath $link
        Write-Output "Avtomatik yonish o'chirildi: kompyuter yonganda bot endi o'zi ishga tushmaydi."
    } else {
        Write-Output "Avtomatik yonish yoqilmagan edi."
    }
    return
}

$root = Split-Path -Parent $PSScriptRoot
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -AtLogon' -f (Join-Path $PSScriptRoot 'start-userbot.ps1')
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7  # minimized
$shortcut.Save()
Write-Output "Tayyor: kompyuter yonganda bot o'zi ishga tushadi (30 soniyadan keyin)."

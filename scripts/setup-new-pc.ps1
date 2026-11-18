# One-time setup on a new Windows computer. Run it in the project folder:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-new-pc.ps1
# It creates .venv, installs the packages and checks what is still missing (Claude Code,
# .env, the Telegram login). It never creates or prints secrets.
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

# 1. Python 3.10+
$python = $null
foreach ($candidate in @('py -3.12', 'py -3', 'python')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    $extra = @($parts | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $version = (& $exe @extra --version 2>&1) | Out-String
    if ($LASTEXITCODE -eq 0 -and $version -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) {
        $python = @($exe) + $extra
        break
    }
}
if (-not $python) {
    Write-Output "Python topilmadi. https://www.python.org/downloads/ dan Python 3.12 ni o'rnating"
    Write-Output "(o'rnatishda 'Add python.exe to PATH' belgisini qo'ying), keyin shu skriptni qayta ishga tushiring."
    exit 1
}
Write-Output "Python: $(& $python[0] @($python | Select-Object -Skip 1) --version 2>&1)"

# 2. Virtual environment + packages
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Output "Virtual muhit yaratilyapti (.venv)..."
    & $python[0] @($python | Select-Object -Skip 1) -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Output "Virtual muhitni yaratib bo'lmadi."; exit 1 }
}
Write-Output "Paketlar o'rnatilyapti (1-3 daqiqa)..."
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet --disable-pip-version-check
& .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Output "Paketlarni o'rnatishda xato chiqdi (internetni tekshiring)."; exit 1 }
Write-Output "Paketlar o'rnatildi."

# 3. What is still missing
$todo = @()
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude -and -not (Test-Path (Join-Path $env:USERPROFILE '.local\bin\claude.exe'))) {
    $todo += "Claude Code o'rnating (PowerShell'da): irm https://claude.ai/install.ps1 | iex"
}
if (-not (Test-Path '.env')) {
    $todo += "Eski kompyuterdagi .env faylini shu papkaga nusxalang (fleshka orqali; Telegram yoki pochta orqali YUBORMANG)."
}
if (-not (Test-Path 'data\userbot.session')) {
    $todo += "Telegram'ga kiring (QR kod chiqadi): .venv\Scripts\python.exe bot.py --userbot-login"
}
$todo += "Eski kompyuterda botni to'xtating: stop-userbot.cmd va scripts\install-autostart.ps1 -Remove"
$todo += "Botni yoqing: start-userbot.cmd"
$todo += "Kompyuter yonganda o'zi yonsin: powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1"

Write-Output ""
Write-Output "Qolgan qadamlar:"
$i = 1
foreach ($step in $todo) { Write-Output ("{0}. {1}" -f $i, $step); $i++ }

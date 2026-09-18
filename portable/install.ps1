$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Get-Command py -ErrorAction SilentlyContinue) -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.12+ не найден. Установите Python с python.org и повторите install.ps1.'
}

$Python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    if ($Python -eq 'py') { & py -3 -m venv .venv } else { & python -m venv .venv }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
New-Item -ItemType Directory -Force -Path 'data','logs' | Out-Null

$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'ArendaKG Lalafo Publisher.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $Root 'start_background.vbs'
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'Arenda.KG Lalafo publisher'
$Shortcut.Save()

Start-Process -FilePath (Join-Path $Root 'start_background.vbs') -WorkingDirectory $Root

Write-Host "Установлено и запущено в фоне. Лог: $Root\logs\bot.log"
Write-Host "Автозапуск создан: $ShortcutPath"

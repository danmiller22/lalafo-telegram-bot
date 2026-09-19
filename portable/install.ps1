$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    foreach ($name in @('python.exe', 'py.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            try {
                $null = & $command.Source --version 2>$null
                if ($LASTEXITCODE -eq 0) { return $command.Source }
            } catch {}
        }
    }
    return $null
}

$PythonExe = Find-Python
if (-not $PythonExe) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Python not found. Installing Python 3.12 automatically...'
        & $winget.Source install --id Python.Python.3.12 -e --scope user --silent --accept-source-agreements --accept-package-agreements
        $PythonExe = Find-Python
    }
}
if (-not $PythonExe) {
    throw 'Python could not be installed automatically. Install Python 3.12+ from python.org and run start.bat again.'
}

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $PythonExe -m venv .venv
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

Write-Host "Installed and started in background. Log: $Root\logs\bot.log"
Write-Host "Startup shortcut: $ShortcutPath"

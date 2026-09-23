$ErrorActionPreference = 'Stop'
$PortableRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $PortableRoot
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
        & $winget.Source install --id Python.Python.3.12 -e --scope user --silent --accept-source-agreements --accept-package-agreements
        $PythonExe = Find-Python
    }
}
if (-not $PythonExe) { throw 'Python 3.12+ is required.' }

if (-not (Test-Path '.venv\Scripts\python.exe')) { & $PythonExe -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if (-not (Test-Path '.env')) { Copy-Item 'portable\.env.collector.example' '.env' }
New-Item -ItemType Directory -Force -Path 'data','logs' | Out-Null

$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'ArendaKG Lalafo Collector.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $PortableRoot 'start_collector_background.vbs'
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'Arenda.KG background Lalafo inventory collector'
$Shortcut.Save()

Start-Process -FilePath (Join-Path $PortableRoot 'start_collector_background.vbs') -WorkingDirectory $Root
Write-Host "Collector installed and started. Log: $Root\logs\collector.log"

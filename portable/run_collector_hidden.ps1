$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $Root)
$ProjectRoot = Get-Location
New-Item -ItemType Directory -Force -Path 'logs','data' | Out-Null
$Log = Join-Path $ProjectRoot 'logs\collector.log'
$OutLog = Join-Path $ProjectRoot 'logs\collector.out.log'
$StopFile = Join-Path $ProjectRoot 'data\collector.stop'
if (Test-Path $StopFile) { Remove-Item -LiteralPath $StopFile -Force }

$Mutex = New-Object Threading.Mutex($false, 'Local\ArendaKGLalafoCollector')
if (-not $Mutex.WaitOne(0, $false)) { exit 0 }
try {
    while (-not (Test-Path $StopFile)) {
        $Process = Start-Process -FilePath (Join-Path $ProjectRoot '.venv\Scripts\python.exe') `
            -ArgumentList '-m','scripts.remote_lalafo_collector' -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $OutLog -RedirectStandardError $Log -PassThru -WindowStyle Hidden
        $Process.WaitForExit()
        if (-not (Test-Path $StopFile)) { Start-Sleep -Seconds 15 }
    }
} finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}

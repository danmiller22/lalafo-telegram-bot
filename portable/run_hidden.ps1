$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
New-Item -ItemType Directory -Force -Path 'logs' | Out-Null
$Log = Join-Path $Root 'logs\bot.log'
$OutLog = Join-Path $Root 'logs\bot.out.log'
$PidFile = Join-Path $Root 'data\bot.pid'
$StopFile = Join-Path $Root 'data\stop.flag'
if (Test-Path $StopFile) { Remove-Item -LiteralPath $StopFile -Force }

$Mutex = New-Object Threading.Mutex($false, 'Local\ArendaKGLalafoPublisher')
if (-not $Mutex.WaitOne(0, $false)) { exit 0 }
try {
    while (-not (Test-Path $StopFile)) {
        $Process = Start-Process -FilePath (Join-Path $Root '.venv\Scripts\python.exe') `
            -ArgumentList '-m','app.bot.lalafo_only' -WorkingDirectory $Root `
            -RedirectStandardOutput $OutLog -RedirectStandardError $Log -PassThru -WindowStyle Hidden
        Set-Content -LiteralPath $PidFile -Value $Process.Id
        $Process.WaitForExit()
        if (-not (Test-Path $StopFile)) { Start-Sleep -Seconds 10 }
    }
} finally {
    if (Test-Path $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}

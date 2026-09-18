$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root 'data\bot.pid'
$StopFile = Join-Path $Root 'data\stop.flag'
New-Item -ItemType File -Force -Path $StopFile | Out-Null
if (Test-Path $PidFile) {
    $BotPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
    if ($BotPid) { Stop-Process -Id ([int]$BotPid) -Force -ErrorAction SilentlyContinue }
}
Write-Host 'Бот остановлен. Для запуска откройте start_background.vbs.'

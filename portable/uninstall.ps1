$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'ArendaKG Lalafo Publisher.lnk'
if (Test-Path $ShortcutPath) { Remove-Item -LiteralPath $ShortcutPath -Force }
Write-Host 'Автозапуск удалён. Файлы проекта и база данных сохранены.'

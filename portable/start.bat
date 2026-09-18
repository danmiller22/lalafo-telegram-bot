@echo off
setlocal
cd /d "%~dp0"
echo Arenda.KG Lalafo bot: установка и запуск...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Ошибка запуска. Проверьте Python и файл logs\bot.log.
  pause
  exit /b 1
)
echo Бот запущен в фоне. Это окно можно закрыть.
timeout /t 3 /nobreak >nul

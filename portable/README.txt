Arenda.KG Lalafo publisher - Windows portable package

1. Распакуйте папку в постоянное место, например C:\ArendaKG.
2. Откройте PowerShell в этой папке и выполните:
   powershell -ExecutionPolicy Bypass -File .\install.ps1
3. Откройте созданный файл .env и заполните TELEGRAM_BOT_TOKEN,
   ADMIN_USER_ID и CALLBACK_SECRET. Используйте отдельного Telegram-бота,
   чтобы не отключать webhook основного облачного Arenda.KG.
4. Запустите .\start_background.vbs. Окно не появится, лог будет в logs\bot.log.

install.ps1 добавляет запуск в автозагрузку текущего пользователя Windows.
Остановка: powershell -ExecutionPolicy Bypass -File .\stop.ps1
Удаление автозапуска: powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

Токен Telegram не хранится в архиве и не передаётся в репозиторий.

Arenda.KG Lalafo link bot - Windows portable package

1. Распакуйте папку в постоянное место, например C:\ArendaKG.
2. Откройте PowerShell в этой папке и выполните:
   powershell -ExecutionPolicy Bypass -File .\install.ps1
3. Откройте созданный файл .env и заполните LALAFO_BOT_TOKEN,
   ADMIN_USER_ID и CALLBACK_SECRET. Токен возьмите у нового бота BotFather,
   чтобы не отключать webhook основного облачного Arenda.KG.
4. Запустите .\start_background.vbs. Окно не появится, лог будет в logs\bot.log.

install.ps1 добавляет запуск в автозагрузку текущего пользователя Windows.
Остановка: powershell -ExecutionPolicy Bypass -File .\stop.ps1
Удаление автозапуска: powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

Это отдельный бот для ссылок Lalafo: администратор отправляет ссылку, бот
спрашивает район и публикует карточку в ту же группу. Основной бот для клиентов
продолжает работать отдельно и не меняется.
Токен Telegram не хранится в архиве и не передаётся в репозиторий.

Важно: один Telegram-токен нельзя одновременно запускать на Koyeb и на этом
ПК. Перед запуском локальной версии остановите облачный процесс, иначе Telegram
будет раздавать обновления между двумя экземплярами.

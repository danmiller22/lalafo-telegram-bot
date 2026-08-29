# Lalafo → Telegram: аренда квартир

Автоматически находит подходящие квартиры в Бишкеке, публикует фотографии и короткую карточку в Telegram-группе, а номер телефона открывает только оплатившему пользователю после ручного подтверждения администратора.

Проект не публикует исходное описание, ссылку Lalafo, автора, тип продавца или телефон. Finik API и автоматическое подтверждение платежа не используются.

## Как устроено

- Semaphore Cloud запускает scraper каждые 2 часа (`0 */2 * * *`) на Python 3.12.
- GitHub Actions проверяет тесты и миграции при изменениях кода, но не обращается к Lalafo.
- Koyeb держит payment bot и health endpoint; scraper работает только в одноразовой VM Semaphore.
- Lalafo читается обычным HTTP через публичные JSON-маршруты `/api/search/v3/feed`, которые использует сама веб-страница; браузер/Playwright не нужен.
- Основная защита от дублей и платежи хранятся в Neon PostgreSQL; `data/posted_ads.json` остаётся резервным state для постоянных окружений.
- Payment bot получает обновления через защищённый Telegram webhook. Входящий запрос будит бесплатный Koyeb после scale-to-zero, поэтому постоянный локальный процесс и keepalive не нужны.
- Production полностью облачный; локальный запуск для эксплуатации не требуется.
- Koyeb держит соединение с чатами Lalafo и на любое новое входящее сообщение
  отвечает одним фиксированным текстом. GitHub Actions будит бесплатный
  scale-to-zero сервис каждые 5 минут.

## 1. Telegram bot и группа

1. Откройте `@BotFather`, создайте бота командой `/newbot` и сохраните token.
2. Добавьте бота в нужную Telegram-группу.
3. Назначьте бота администратором с правами отправлять сообщения, фотографии и удалять собственные сообщения.
4. Напишите боту в личку `/start`, затем `/myid`.
5. Скопируйте число из `Ваш Telegram ID: ...` в `ADMIN_USER_ID`.

Token нельзя добавлять в исходники, README или `posted_ads.json`.

## 2. Neon PostgreSQL

1. Создайте бесплатный проект на [Neon](https://console.neon.tech/).
2. В панели проекта скопируйте pooled connection string PostgreSQL.
3. Сохраните её как `DATABASE_URL`. Поддерживаются оба варианта:
   - `postgresql://user:password@host/database?sslmode=require`
   - `postgres://user:password@host/database?sslmode=require`
4. Примените схему: `alembic upgrade head`.

Для локального smoke test без Neon используйте `DATABASE_URL=sqlite:///data/bot.db`.

## 3. Опциональная среда разработчика

Требуется Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Заполните `.env`:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_USER_ID`
- `CALLBACK_SECRET` — случайная строка не короче 16 символов
- `DATABASE_URL`

`.env` уже добавлен в `.gitignore`.

## 4. Безопасная проверка scraper

Оставьте в `.env`:

```dotenv
DRY_RUN=true
TEST_MODE=true
```

Одна команда безопасного теста:

```bash
python -m scripts.scrape_publish
```

Она покажет количество найденных объявлений, извлечённые поля, маскированный телефон и preview карточки. Telegram, база и state не изменятся.

После проверки можно установить `DRY_RUN=false`. `TEST_MODE=true` ограничивает публикацию одной квартирой. Не выключайте оба флага одновременно при первом live-запуске.

## 5. Запуск payment bot

```bash
alembic upgrade head
python -m app.bot
```

Команды:

- `/start` — начало работы;
- `/myid` — показать numeric Telegram ID;
- `/status` — health check;
- `/admin`, `/pending`, `/stats` — только для `ADMIN_USER_ID`.

### Проверка flow оплаты

1. Нажмите `🔐 Посмотреть номер` под квартирой.
2. В личном чате бот покажет цену контакта 100 сом и кнопки оплаты и проверки.
3. Оплатите по ссылке и нажмите `✅ Проверить оплату`.
4. Администратор получает push-карточку с `Подтвердить/Отказать`.
5. После подтверждения бот автоматически отправит покупателю в личку полную
   карточку квартиры с номером собственника. В группе номер не публикуется.

### Заявка «Ищу квартиру»

1. В личном чате с ботом нажмите `🔎 Разместить «Ищу квартиру»` или отправьте `/want`.
2. Укажите комнаты, район, бюджет, срок заселения, жильцов, пожелания и контакт.
3. Проверьте предварительный вид заявки и оплатите публикацию — 100 сом.
4. Нажмите `✅ Проверить оплату`.
5. После подтверждения администратора заявка автоматически публикуется в группе,
   а клиент получает уведомление в личном чате.

## 6. Облачная production-конфигурация

### Semaphore Secret

Создайте Secret, доступный только проекту `lalafo-telegram-bot`:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`
- `CALLBACK_SECRET`
- `ADMIN_USER_ID`
- `TELEGRAM_GROUP_ID`

Secret называется `lalafo-production`. Scheduled Task запускает только `.semaphore/production.yml`; обычные push запускают безопасный `.semaphore/semaphore.yml` с `DRY_RUN=true`.

В production pipeline используются:

- `DRY_RUN=false`
- `TEST_MODE=true` для первого контролируемого запуска, затем `false`.

### Koyeb service

Koyeb запускает `uvicorn app.web:app` и получает те же Telegram/Neon secrets, а также:

- `RUN_BOT=true`
- `TELEGRAM_WEBHOOK_URL=https://statutory-mallissa-2danmiller-f1c1b08d.koyeb.app/telegram/webhook`;
- `TELEGRAM_WEBHOOK_SECRET` — отдельная URL-safe строка не короче 32 символов;
- `RUN_TRIGGER_SECRET` — случайная строка не короче 32 символов;
- `DRY_RUN=true` — HTTP-service сам не публикует объявления.
- `LALAFO_AUTO_REPLY_ENABLED=true`;
- `LALAFO_LOGIN` и `LALAFO_PASSWORD` — только в защищённых secrets Koyeb;
- `LALAFO_AUTO_REPLY_POLL_SECONDS=10`.

При старте Koyeb платёжный Telegram-бот становится готов принимать клиентские webhook сразу, а сетевое обновление webhook и меню команд завершается в фоне с автоматическими повторами. Telegram повторяет доставку при временной недоступности сервиса. `GET /health` отражает прежде всего готовность payment bot: временный сбой автоответчика Lalafo или планировщика квартир больше не перезапускает оплату и выдачу доступов. Scraper в Semaphore вызывает health endpoint после каждого успешного цикла.

Для бесплатного Koyeb включён лёгкий внутренний keepalive: раз в 15 минут сервис
обращается к собственному публичному `/health`, а GitHub Actions остаётся независимым
резервным ping. Отдельный watchdog каждые 30 секунд проверяет Telegram setup,
планировщик квартир, Lalafo auto-reply и keepalive. Если одна задача завершилась,
перезапускается только она; платёжный runtime и остальные процессы не затрагиваются.
Состояние обоих механизмов доступно в `/health` как `free_cloud_keepalive` и
`background_watchdog`.

## 7. Активация публикации

1. Выполните Semaphore workflow с `DRY_RUN=true` и убедитесь, что Lalafo отвечает `200`, телефон замаскирован, а Telegram/БД не меняются.
2. Добавьте production secrets в Semaphore и Koyeb.
3. Запустите Koyeb с `RUN_BOT=true` и проверьте `/health`: `{"status":"ok","bot":"running"}`.
4. Для первой публикации оставьте `TEST_MODE=true`, установите `DRY_RUN=false` только в Semaphore и запустите Task вручную.
5. Проверьте одну карточку и полный flow оплаты. После этого установите `TEST_MODE=false`.
6. Активный Scheduled Task выполняется каждые 2 часа.

## 8. Тесты

```bash
pytest -q
```

Покрываются депозит, нормализация телефона, фильтры, deduplication, callback signatures, pending/approve/reject, повторная проверка, admin-only approval и доступ к номеру.

## Troubleshooting

- **Lalafo 403/429:** scraper не обходит защиту, ждёт с backoff и завершает run с понятным логом. State не очищается.
- **Облачный runner получает 403:** не используйте proxy/CAPTCHA bypass; смените легитимный runner. Проверенный для этого проекта runner — Semaphore Cloud.
- **Не приходит admin card:** проверьте `ADMIN_USER_ID`; username недостаточен для Bot API.
- **Telegram не скачал фото:** scraper повторит album с основной фотографией; объявление без доступного фото не публикуется.
- **Бот отвечает `Квартира больше недоступна`:** запись отсутствует, выключена или не содержит телефона.
- **Neon connection error:** используйте pooled URL и обязательный TLS-параметр провайдера.
- **Duplicate message после сбоя push:** PostgreSQL дополнительно блокирует повторную публикацию по Lalafo ID/fingerprint.
- **State conflict:** workflow сериализован через concurrency и делает `git pull --rebase` перед push.

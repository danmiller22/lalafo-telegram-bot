# Lalafo → Telegram: аренда квартир

Автоматически находит подходящие квартиры в Бишкеке, публикует фотографии и короткую карточку в Telegram-группе, а номер телефона открывает только оплатившему пользователю после ручного подтверждения администратора.

Проект не публикует исходное описание, ссылку Lalafo, автора, тип продавца или телефон. Finik API и автоматическое подтверждение платежа не используются.

## Как устроено

- GitHub Actions запускает scraper в `17` и `47` минут каждого часа.
- Lalafo читается обычным HTTP из серверного Next.js JSON `__NEXT_DATA__`; браузер/Playwright не нужен.
- История публикаций хранится в `data/posted_ads.json`, а квартиры и платежи — в PostgreSQL.
- Payment bot работает постоянно через aiogram long polling: `python -m app.bot`.
- Локально можно использовать SQLite, production-конфигурация рассчитана на Neon PostgreSQL.

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

## 3. Локальная настройка

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

1. Нажмите `📞 Получить номер` под квартирой.
2. Откройте приватный flow и нажмите `💳 Оплатить`.
3. После оплаты нажмите `✅ Я оплатил`.
4. Администратор получает карточку с `Подтвердить/Отказать`.
5. После подтверждения вернитесь к исходной квартире и снова нажмите `Получить номер`.
6. Номер появится только в персональном callback alert, не в группе и не в личном сообщении.

## 6. GitHub repository и Secrets

Создайте repository, загрузите проект и откройте **Settings → Secrets and variables → Actions**.

Secrets:

- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL`
- `CALLBACK_SECRET`
- `ADMIN_USER_ID`

Variables:

- `TELEGRAM_GROUP_ID=-1004389602150`
- `DRY_RUN=true`
- `TEST_MODE=true`

Workflow уже имеет `contents: write`, `concurrency` и commit/push только при изменении state.

## 7. Первый запуск GitHub Actions

1. Откройте вкладку **Actions**.
2. Выберите **Lalafo scraper**.
3. Нажмите **Run workflow**.
4. Убедитесь в логах, что `DRY_RUN enabled` и телефон замаскирован.
5. Для первой реальной публикации оставьте `TEST_MODE=true`, переключите `DRY_RUN=false` и снова нажмите **Run workflow**.
6. После проверки одной карточки можно переключить `TEST_MODE=false`. Расписание включается автоматически после появления workflow в default branch.

## 8. Тесты

```bash
pytest -q
```

Покрываются депозит, нормализация телефона, фильтры, deduplication, callback signatures, pending/approve/reject, повторная проверка, admin-only approval и доступ к номеру.

## Troubleshooting

- **Lalafo 403/429:** scraper не обходит защиту, ждёт с backoff и завершает run с понятным логом. State не очищается.
- **Не приходит admin card:** проверьте `ADMIN_USER_ID`; username недостаточен для Bot API.
- **Telegram не скачал фото:** scraper повторит album с основной фотографией; объявление без доступного фото не публикуется.
- **Бот отвечает `Квартира больше недоступна`:** запись отсутствует, выключена или не содержит телефона.
- **Neon connection error:** используйте pooled URL и обязательный TLS-параметр провайдера.
- **Duplicate message после сбоя push:** PostgreSQL дополнительно блокирует повторную публикацию по Lalafo ID/fingerprint.
- **State conflict:** workflow сериализован через concurrency и делает `git pull --rebase` перед push.

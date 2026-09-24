from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import TermsConsent


TERMS_VERSION = "2026-09-24-v1"
TERMS_ACCEPT_BUTTON = "✅ Я ознакомлен(а) с условиями и согласен(на) со всеми пунктами"
TERMS_TEXT = (
    "⚠️ Условия и правила безопасности\n\n"
    "Объявления собираются из открытых источников. Мы не проверяем личность "
    "автора и не гарантируем, что отметка «собственник», «агент» или «неизвестно» "
    "указана автором правдиво. Риелторы и мошенники могут представляться "
    "агентами или владельцами квартиры.\n\n"
    "Не переводите задаток, бронь или предоплату до личного просмотра квартиры "
    "и проверки документов. Сверьте личность человека, его право собственности "
    "или право сдавать квартиру, проверьте оригиналы документов и заключите "
    "письменный договор. Передавайте деньги только после проверки условий и "
    "сохраняйте подтверждение оплаты. Не сообщайте коды из SMS, данные карты и "
    "пароли.\n\n"
    "Оплата сервиса предоставляет доступ к контактам объявлений и не гарантирует "
    "актуальность квартиры, личность автора или успешное заключение сделки."
)

PRIVACY_TEXT = (
    "🔒 Политика конфиденциальности Arenda.KG\n\n"
    "Сервис сохраняет Telegram ID, имя и username, выбранные квартиры, статусы "
    "оплаты, предоставленные чеки и сообщения в техподдержку. Эти данные нужны "
    "для идентификации пользователя, выдачи доступа, проверки оплаты, защиты "
    "сервиса и ответа на обращения.\n\n"
    "Мы не продаём персональные данные. Они передаются техническим и платёжным "
    "поставщикам только в объёме, необходимом для работы сервиса, либо когда этого "
    "требует закон. Платёжные записи хранятся в пределах необходимых бухгалтерских "
    "и юридических сроков.\n\n"
    "Чтобы задать вопрос о данных или запросить их удаление, откройте "
    "«Техподдержку» в этом боте."
)


class TermsConsentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def accepted(self, telegram_user_id: int) -> bool:
        async with self.sessions() as session:
            row = await session.get(TermsConsent, telegram_user_id)
            return bool(row and row.accepted and row.version == TERMS_VERSION)

    async def accept(self, telegram_user_id: int) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(TermsConsent, telegram_user_id)
            now = datetime.now(timezone.utc)
            if row is None:
                session.add(
                    TermsConsent(
                        telegram_user_id=telegram_user_id,
                        version=TERMS_VERSION,
                        accepted=True,
                        accepted_at=now,
                    )
                )
            else:
                row.version = TERMS_VERSION
                row.accepted = True
                row.accepted_at = now

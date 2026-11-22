"""Send a brief reply to open Telegram support complaints."""

import asyncio
import re

from aiogram import Bot
from sqlalchemy import String, cast, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import LalafoAutoReplyMeta, SupportTicket

PREFIX = "support-refund-reply:"
REPLY = (
    "Здравствуйте! Сожалеем, что возникла проблема. "
    "По условиям договора возможен частичный возврат. "
    "После проверки можем рассмотреть возврат 50% оплаты."
)
COMPLAINT = re.compile(
    r"\b(?:претензи\w*|жалоб\w*|обман\w*|мошеннич\w*)\b"
    r"|\b(?:верните|вернуть|возврат)\s+(?:мне\s+)?(?:деньги|денег|средства|средств|оплату|оплаты)\b"
    r"|\bоплатил[аи]?\b.{0,100}\b(?:нет доступа|доступ не|не работает|не получил\w*)\b",
    re.IGNORECASE | re.DOTALL,
)


async def send_replies(sessions, bot) -> tuple[int, int]:
    async with sessions() as session:
        tickets = (await session.scalars(
            select(SupportTicket)
            .where(
                SupportTicket.status == "open",
                ~select(LalafoAutoReplyMeta.key)
                .where(LalafoAutoReplyMeta.key == PREFIX + cast(SupportTicket.id, String))
                .exists(),
            )
            .order_by(SupportTicket.id)
        )).all()
    sent = failed = 0
    for ticket in tickets:
        if not COMPLAINT.search(ticket.question):
            continue
        key = PREFIX + str(ticket.id)
        try:
            async with sessions.begin() as session:
                current = await session.get(SupportTicket, ticket.id)
                if current is None or current.status != "open":
                    continue
                session.add(LalafoAutoReplyMeta(key=key, value="sending"))
        except IntegrityError:
            continue
        try:
            await bot.send_message(ticket.telegram_user_id, REPLY)
        except Exception:
            # A timeout can follow successful delivery. Keep the reservation.
            failed += 1
            continue
        sent += 1
        async with sessions.begin() as session:
            marker = await session.get(LalafoAutoReplyMeta, key)
            marker.value = "sent"
    return sent, failed


async def main() -> None:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    try:
        sent, failed = await send_replies(sessions, bot)
        print(f"Support replies: sent={sent}, check_delivery={failed}")
    except Exception as exc:
        raise SystemExit(f"Support check failed: {type(exc).__name__}") from None
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment
from app.security import TokenSigner
from app.telegram.keyboards import apartment_keyboard


logger = logging.getLogger(__name__)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    signer = TokenSigner(settings.require_callback_secret())
    bot = Bot(token=settings.require_bot_token())
    updated = 0
    unchanged = 0
    failed = 0
    try:
        async with sessions() as session:
            apartments = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.publication_status == "published",
                            Apartment.telegram_chat_id.is_not(None),
                            Apartment.telegram_message_id.is_not(None),
                        )
                    )
                ).all()
            )
        for apartment in apartments:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=apartment.telegram_chat_id,
                    message_id=apartment.telegram_message_id,
                    reply_markup=apartment_keyboard(
                        apartment.id,
                        signer=signer,
                        support_url=settings.support_url,
                        payment_url=settings.finik_payment_url,
                    ),
                )
                updated += 1
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    unchanged += 1
                    continue
                failed += 1
                logger.warning(
                    "Could not update apartment keyboard id=%s: %s",
                    apartment.id,
                    type(exc).__name__,
                )
        logger.info(
            "Published keyboards synced: updated=%d unchanged=%d failed=%d",
            updated,
            unchanged,
            failed,
        )
        if apartments and updated == 0 and unchanged == 0:
            return 1
        return 0
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

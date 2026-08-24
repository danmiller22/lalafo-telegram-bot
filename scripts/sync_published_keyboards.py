from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION, apartment_keyboard


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
    skipped = 0
    failed = 0
    try:
        async with sessions.begin() as session:
            apartments = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.publication_status == "published",
                            Apartment.telegram_chat_id.is_not(None),
                            Apartment.telegram_message_id.is_not(None),
                            Apartment.keyboard_version < APARTMENT_KEYBOARD_VERSION,
                        )
                    )
                ).all()
            )
            for apartment in apartments:
                if apartment.deposit == 1:
                    apartment.deposit = None
        for apartment in apartments:
            for attempt in range(5):
                try:
                    await bot.edit_message_text(
                        chat_id=apartment.telegram_chat_id,
                        message_id=apartment.telegram_message_id,
                        text=format_public_apartment(
                            apartment,
                            bot_username=settings.telegram_bot_username,
                        ),
                        reply_markup=apartment_keyboard(
                            apartment.id,
                            signer=signer,
                            bot_username=settings.telegram_bot_username,
                            support_url=settings.support_url,
                        ),
                    )
                    updated += 1
                    apartment.keyboard_version = APARTMENT_KEYBOARD_VERSION
                    break
                except TelegramRetryAfter as exc:
                    if attempt == 4:
                        failed += 1
                        logger.warning(
                            "Could not update apartment keyboard id=%s after rate-limit retries",
                            apartment.id,
                        )
                        break
                    wait_seconds = max(float(exc.retry_after), 1.0) + 1.0
                    logger.info(
                        "Telegram rate limit while syncing cards; retrying in %.0f seconds",
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
                except TelegramBadRequest as exc:
                    error_text = str(exc).lower()
                    if "message is not modified" in error_text:
                        unchanged += 1
                        apartment.keyboard_version = APARTMENT_KEYBOARD_VERSION
                        break
                    if any(
                        reason in error_text
                        for reason in (
                            "message to edit not found",
                            "message can't be edited",
                            "chat not found",
                        )
                    ):
                        skipped += 1
                        apartment.keyboard_version = APARTMENT_KEYBOARD_VERSION
                        logger.info(
                            "Skipping unavailable legacy apartment card id=%s: %s",
                            apartment.id,
                            exc,
                        )
                        break
                    failed += 1
                    logger.warning(
                        "Could not update apartment keyboard id=%s: %r",
                        apartment.id,
                        exc,
                    )
                    break
        synced_ids = [
            apartment.id
            for apartment in apartments
            if apartment.keyboard_version == APARTMENT_KEYBOARD_VERSION
        ]
        if synced_ids:
            async with sessions.begin() as session:
                await session.execute(
                    update(Apartment)
                    .where(Apartment.id.in_(synced_ids))
                    .values(keyboard_version=APARTMENT_KEYBOARD_VERSION)
                )
        logger.info(
            "Published keyboards synced: updated=%d unchanged=%d skipped=%d failed=%d",
            updated,
            unchanged,
            skipped,
            failed,
        )
        if apartments and updated == 0 and unchanged == 0 and skipped == 0:
            return 1
        return 0
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

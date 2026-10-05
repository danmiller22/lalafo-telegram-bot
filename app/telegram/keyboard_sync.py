"""Refresh published card buttons after a keyboard version change."""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select, update

from app.models import Apartment
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION, apartment_keyboard

logger = logging.getLogger(__name__)


async def sync_published_keyboards(runtime) -> None:
    sessions = runtime.workflow_data["apartments"].sessions
    settings = runtime.workflow_data["settings"]
    signer = runtime.workflow_data["signer"]
    last_id = 0
    updated = 0
    while True:
        async with sessions() as session:
            rows = (await session.execute(select(
                Apartment.id, Apartment.telegram_chat_id, Apartment.telegram_message_id
            ).where(
                Apartment.id > last_id,
                Apartment.keyboard_version < APARTMENT_KEYBOARD_VERSION,
                Apartment.telegram_chat_id.is_not(None),
                Apartment.telegram_message_id.is_not(None),
            ).order_by(Apartment.id).limit(50))).all()
        if not rows:
            break
        for apartment_id, chat_id, message_id in rows:
            last_id = apartment_id
            markup = apartment_keyboard(apartment_id, signer=signer,
                bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url)
            try:
                while True:
                    try:
                        await runtime.bot.edit_message_reply_markup(
                            chat_id=chat_id, message_id=message_id, reply_markup=markup)
                        break
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(exc.retry_after + 1)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.warning("Cannot refresh apartment %s buttons: %s", apartment_id, exc)
                    continue
            except Exception:
                logger.exception("Could not refresh apartment %s buttons", apartment_id)
                continue
            async with sessions.begin() as session:
                await session.execute(update(Apartment).where(Apartment.id == apartment_id).values(
                    keyboard_version=APARTMENT_KEYBOARD_VERSION))
            updated += 1
            await asyncio.sleep(0.1)
    logger.info("Published apartment keyboards refreshed: %s", updated)

"""One-time cleanup for the confirmed duplicate cards from September 23."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Apartment, ApartmentInventoryQueue
from app.state import normalized_district


logger = logging.getLogger(__name__)
JOYKA_ID_MIN = -8_000_000_000_000_000_000
JOYKA_ID_MAX = JOYKA_ID_MIN + 1_000_000_000


def confirmed_duplicate_message_ids(
    original: Apartment | None,
    repeated: Apartment | None,
    *,
    chat_id: int,
) -> list[int]:
    if original is None or repeated is None:
        return []
    if (
        original.id != 1597
        or repeated.id != 1386
        or original.lalafo_id != JOYKA_ID_MIN + 532730
        or repeated.lalafo_id != 116628443
        or original.telegram_chat_id != chat_id
        or repeated.telegram_chat_id != chat_id
        or original.telegram_message_id != 35550
        or repeated.telegram_message_id != 35555
        or original.phone != repeated.phone
        or original.price != repeated.price
        or original.rooms != repeated.rooms
        or normalized_district(original.district) != "восток5"
        or normalized_district(repeated.district) != "восток5"
    ):
        return []
    first_photos = [urlsplit(url).path.rsplit("/", 1)[-1] for url in original.photo_urls]
    second_photos = [urlsplit(url).path.rsplit("/", 1)[-1] for url in repeated.photo_urls]
    if len(first_photos) != 4 or first_photos != second_photos:
        return []
    return list(range(35551, 35556))


async def run(
    bot: Bot,
    sessions: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
) -> None:
    async with sessions.begin() as session:
        joyka_ids = select(Apartment.id).where(
            Apartment.lalafo_id.between(JOYKA_ID_MIN, JOYKA_ID_MAX)
        )
        result = await session.execute(
            update(ApartmentInventoryQueue)
            .where(
                ApartmentInventoryQueue.apartment_id.in_(joyka_ids),
                ApartmentInventoryQueue.status == "queued",
            )
            .values(status="skipped", last_error="retired_joyka_source")
        )
        logger.info("Skipped retired Joyka queue items count=%s", result.rowcount)

    async with sessions() as session:
        original = await session.get(Apartment, 1597)
        repeated = await session.get(Apartment, 1386)
    message_ids = confirmed_duplicate_message_ids(original, repeated, chat_id=chat_id)
    if not message_ids:
        logger.info("Confirmed duplicate already removed or source details changed")
        return

    deleted = await bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
    if not deleted:
        raise RuntimeError("Telegram did not confirm duplicate message deletion")
    async with sessions.begin() as session:
        await session.execute(
            update(Apartment)
            .where(Apartment.id == 1386, Apartment.telegram_message_id == 35555)
            .values(
                telegram_message_id=None,
                publication_status="duplicate",
                active=False,
            )
        )
        await session.execute(
            update(ApartmentInventoryQueue)
            .where(ApartmentInventoryQueue.apartment_id == 1386)
            .values(status="skipped", last_error="confirmed_duplicate_removed")
        )
    logger.info("Removed confirmed duplicate album and card message_ids=%s", message_ids)

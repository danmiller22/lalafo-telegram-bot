from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.featured.repository import FeaturedRepository
from app.featured.telegram import apartment_to_ad
from app.models import Apartment
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if not settings.featured_lalafo_enabled:
        logging.info("Featured automation is disabled; no-op")
        return 0
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    repo = FeaturedRepository(sessions)
    business_date = datetime.now(ZoneInfo(settings.featured_timezone)).date()
    rows = await repo.for_date(business_date)
    if not rows:
        logging.info("No featured apartments for %s; no-op", business_date)
        await engine.dispose()
        return 0
    bot = Bot(token=settings.require_bot_token())
    publisher = TelegramPublisher(
        bot, chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_url, max_photos=settings.featured_max_photos,
    )
    try:
        for row in rows:
            try:
                async with sessions() as session:
                    apartment = await session.get(Apartment, row.source_apartment_id)
                if apartment is None:
                    raise LookupError("Featured apartment is missing")
                message = await publisher.publish(apartment.id, apartment_to_ad(apartment))
                await repo.mark_repeat(row.id, message.message_id, settings.telegram_group_id)
            except Exception as exc:
                logging.error("Featured repeat slot=%s failed safely: %s", row.slot, type(exc).__name__)
    finally:
        await bot.session.close()
        await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

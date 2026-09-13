from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.models import Apartment, DailyFeaturedPublication
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher


logger = logging.getLogger(__name__)
BISHKEK = ZoneInfo("Asia/Bishkek")
ACTIVE_MANAGED_TERMS = {
    116308426: (26_000, "Восток-5"),
    116308347: (40_000, "Восток-5"),
}
MIN_GLOBAL_REPEAT_GAP = timedelta(hours=3, minutes=30)
REPEAT_HOURS = frozenset({0, 4, 8, 12, 16, 20})


def is_repeat_window(now: datetime, last_repeat_at: datetime | None) -> bool:
    local_now = now.astimezone(BISHKEK)
    if local_now.hour not in REPEAT_HOURS:
        return False
    if last_repeat_at is None:
        return True
    if last_repeat_at.tzinfo is None:
        last_repeat_at = last_repeat_at.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) - last_repeat_at.astimezone(
        timezone.utc
    ) >= MIN_GLOBAL_REPEAT_GAP


def owner_card(apartment: Apartment, managed_id: int) -> LalafoAd:
    price, district = ACTIVE_MANAGED_TERMS[managed_id]
    return LalafoAd(
        lalafo_id=apartment.lalafo_id,
        source_url=apartment.source_url,
        phone=apartment.phone,
        price=price,
        currency="KGS",
        rooms=apartment.rooms,
        district=district,
        city=apartment.city,
        deposit=None,
        photo_urls=list(apartment.photo_urls),
        category_id=2044,
        no_subletting=apartment.no_subletting,
        owner_listing=apartment.owner_listing,
    )


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    bot: Bot | None = None
    try:
        await init_db(engine)
        async with sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(DailyFeaturedPublication, Apartment)
                        .join(
                            Apartment,
                            Apartment.id
                            == DailyFeaturedPublication.source_apartment_id,
                        )
                        .where(
                            DailyFeaturedPublication.managed_lalafo_ad_id.in_(
                                ACTIVE_MANAGED_TERMS
                            ),
                            DailyFeaturedPublication.deactivated_at.is_(None),
                            Apartment.active.is_(True),
                        )
                    )
                ).all()
            )
        verified = [
            (row, apartment)
            for row, apartment in rows
            if apartment.phone
            and apartment.phone_source_version == PHONE_SOURCE_VERSION
            and len(apartment.photo_urls) >= 2
        ]
        if not verified:
            logger.info("No verified active managed originals; no-op")
            return 0

        now = datetime.now(timezone.utc)
        latest = max(
            (row.last_telegram_repeat_at for row, _ in verified),
            default=None,
            key=lambda value: value or datetime.min.replace(tzinfo=timezone.utc),
        )
        if not is_repeat_window(now, latest):
            logger.info("Managed repeat is not due; no-op")
            return 0

        row, apartment = min(
            verified,
            key=lambda item: item[0].last_telegram_repeat_at
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        bot = Bot(token=settings.require_bot_token())
        publisher = TelegramPublisher(
            bot,
            chat_id=settings.telegram_group_id,
            signer=TokenSigner(settings.require_callback_secret()),
            bot_username=settings.telegram_bot_username,
            support_url=settings.support_url,
            max_photos=settings.max_photos_per_apartment,
        )
        try:
            message = await publisher.publish(
                apartment.id,
                owner_card(apartment, row.managed_lalafo_ad_id),
            )
        except TelegramPublishError as exc:
            logger.error("Managed Telegram repeat failed safely: %s", exc)
            return 2

        async with sessions.begin() as session:
            stored = await session.get(DailyFeaturedPublication, row.id)
            if stored is not None:
                stored.last_telegram_repeat_at = now
                stored.telegram_message_id = message.message_id
                stored.telegram_chat_id = settings.telegram_group_id
        logger.info(
            "MANAGED_REPEAT_PUBLISHED managed_id=%s source_id=%s message_id=%s",
            row.managed_lalafo_ad_id,
            apartment.lalafo_id,
            message.message_id,
        )
        return 0
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

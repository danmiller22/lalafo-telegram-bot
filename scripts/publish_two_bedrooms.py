from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.client import LalafoClient, LalafoError
from app.lalafo.exclusions import is_permanently_excluded
from app.lalafo.models import LalafoAd
from app.lalafo.parser import LalafoParseError, is_allowed
from app.models import Apartment
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from scripts.scrape_publish import (
    SOURCE_MAX_SEARCH_PAGES,
    SOURCE_PUBLISH_SPACING_SECONDS,
    deduplicate_candidates,
    fetch_detail_batch,
    select_publish_batch,
)


logger = logging.getLogger(__name__)
BISHKEK = ZoneInfo("Asia/Bishkek")
TWO_BEDROOM_SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/2-bedrooms/owner"
    "?price[from]=20000&price[to]=40000"
)
TWO_BEDROOM_DAILY_LIMIT = 20
TWO_BEDROOM_SLOT_LIMIT = 5
TWO_BEDROOM_PRIMARY_MIN_PHOTOS = 4
TWO_BEDROOM_FALLBACK_MIN_PHOTOS = 1
TWO_BEDROOM_MIN_PRICE = 20_000
TWO_BEDROOM_MAX_PRICE = 40_000
TWO_BEDROOM_CANDIDATE_LIMIT = 100
TWO_BEDROOM_SLOT_HOURS = (9, 13, 17, 21)


def bishkek_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    local_date = current.astimezone(BISHKEK).date()
    start = datetime.combine(local_date, time.min, tzinfo=BISHKEK)
    end = datetime.combine(local_date, time.max, tzinfo=BISHKEK)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def bishkek_slot_target(now: datetime | None = None) -> tuple[str, int] | None:
    current = (now or datetime.now(timezone.utc)).astimezone(BISHKEK)
    passed = [hour for hour in TWO_BEDROOM_SLOT_HOURS if current.hour >= hour]
    if not passed:
        return None
    hour = passed[-1]
    slot_number = TWO_BEDROOM_SLOT_HOURS.index(hour) + 1
    return f"{current.date().isoformat()}:{hour:02d}", slot_number * TWO_BEDROOM_SLOT_LIMIT


async def published_two_bedrooms_today(sessions, *, now: datetime | None = None) -> int:
    start, end = bishkek_day_window(now)
    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Apartment)
                .where(
                    Apartment.publication_status == "published",
                    Apartment.rooms == "2",
                    Apartment.published_at.is_not(None),
                    Apartment.published_at >= start,
                    Apartment.published_at <= end,
                )
            )
            or 0
        )


def select_two_bedroom_batch(candidates: list[LalafoAd], limit: int) -> list[LalafoAd]:
    eligible = []
    for ad in deduplicate_candidates(candidates):
        allowed, _ = is_allowed(
            ad,
            city="Бишкек",
            max_price=TWO_BEDROOM_MAX_PRICE,
            rooms=("2",),
        )
        if (
            allowed
            and ad.owner_listing
            and TWO_BEDROOM_MIN_PRICE <= ad.price <= TWO_BEDROOM_MAX_PRICE
            and len(ad.photo_urls) >= TWO_BEDROOM_FALLBACK_MIN_PHOTOS
            and not is_permanently_excluded(ad.lalafo_id)
        ):
            eligible.append(ad)

    primary = [
        ad for ad in eligible if len(ad.photo_urls) >= TWO_BEDROOM_PRIMARY_MIN_PHOTOS
    ]
    selected = select_publish_batch(primary, min(limit, len(primary)))
    if len(selected) < limit:
        selected_ids = {ad.lalafo_id for ad in selected}
        fallback = [ad for ad in eligible if ad.lalafo_id not in selected_ids]
        selected.extend(select_publish_batch(fallback, limit - len(selected)))
    return selected


async def run(*, target_today: int | None = None) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        token = settings.require_bot_token()
        callback_secret = settings.require_callback_secret()
    except RuntimeError as exc:
        logger.error("Production configuration is incomplete: %s", exc)
        return 2

    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    already_published = await published_two_bedrooms_today(sessions)
    if target_today is None:
        slot = bishkek_slot_target()
        if slot is None:
            logger.info("Two-bedroom publication is before the first Bishkek slot")
            await engine.dispose()
            return 0
        _, target_today = slot
    target_today = max(0, min(TWO_BEDROOM_DAILY_LIMIT, target_today))
    remaining = max(0, target_today - already_published)
    limit = min(TWO_BEDROOM_SLOT_LIMIT, remaining)
    if not limit:
        logger.info(
            "Two-bedroom slot already satisfied: published_today=%d target=%d",
            already_published,
            target_today,
        )
        await engine.dispose()
        return 0

    candidates: list[LalafoAd] = []
    candidate_ids: set[int] = set()
    try:
        async with AsyncExitStack() as stack:
            search_client = await stack.enter_async_context(
                LalafoClient(
                    timeout=settings.http_timeout_seconds,
                    max_retries=settings.http_max_retries,
                    proxy_url=settings.lalafo_proxy_url,
                )
            )
            detail_clients = [
                await stack.enter_async_context(
                    LalafoClient(
                        timeout=settings.http_timeout_seconds,
                        max_retries=settings.http_max_retries,
                        proxy_url=settings.lalafo_proxy_url,
                    )
                )
                for _ in range(max(1, min(12, settings.apartment_detail_concurrency)))
            ]
            for page_number in range(1, SOURCE_MAX_SEARCH_PAGES + 1):
                try:
                    page = await search_client.search(
                        TWO_BEDROOM_SEARCH_URL,
                        page=page_number,
                    )
                except (LalafoError, LalafoParseError) as exc:
                    if candidates:
                        logger.warning(
                            "Two-bedroom search stopped after partial collection: %s",
                            type(exc).__name__,
                        )
                        break
                    raise
                if page_number == 1:
                    logger.info("Two-bedroom Lalafo search found %d advertisements", page.total)
                if not page.items:
                    break
                page.items.sort(
                    key=lambda item: item.updated_at.timestamp() if item.updated_at else 0,
                    reverse=True,
                )
                source_items = [
                    item
                    for item in page.items
                    if item.lalafo_id not in candidate_ids
                    and not is_permanently_excluded(item.lalafo_id)
                    and (
                        not item.price
                        or TWO_BEDROOM_MIN_PRICE
                        <= item.price
                        <= TWO_BEDROOM_MAX_PRICE
                    )
                    and item.photo_urls
                ]
                published_ids = await apartments.published_lalafo_ids(
                    [item.lalafo_id for item in source_items]
                )
                details = await fetch_detail_batch(
                    [item for item in source_items if item.lalafo_id not in published_ids],
                    detail_clients,
                )
                parsed = [ad for _, ad in details if ad is not None]
                duplicate_ids = await apartments.duplicate_candidate_ids(parsed)
                for _, ad in details:
                    if ad is None or ad.lalafo_id in duplicate_ids:
                        continue
                    candidates.append(ad)
                    candidate_ids.add(ad.lalafo_id)
                if len(candidates) >= TWO_BEDROOM_CANDIDATE_LIMIT:
                    break
                if page_number >= page.page_count:
                    break
    except (LalafoError, LalafoParseError) as exc:
        logger.error("Two-bedroom search failed safely: %s", type(exc).__name__)
        await engine.dispose()
        return 2

    selected = select_two_bedroom_batch(candidates, limit)
    logger.info(
        "Two-bedroom candidates selected: selected=%d slot_limit=%d daily_remaining=%d",
        len(selected),
        TWO_BEDROOM_SLOT_LIMIT,
        remaining,
    )
    if not selected:
        await engine.dispose()
        return 0

    bot = Bot(token=token)
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=TokenSigner(callback_secret),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,
        max_photos=settings.max_photos_per_apartment,
    )
    published = 0
    failures = 0
    try:
        for index, ad in enumerate(selected):
            if await apartments.is_duplicate(ad):
                logger.info("Skipping permanent DB duplicate id=%s", ad.lalafo_id)
                continue
            if await published_two_bedrooms_today(sessions) >= TWO_BEDROOM_DAILY_LIMIT:
                logger.info("Two-bedroom daily limit reached during publication")
                break
            apartment = await apartments.upsert_discovered(ad)
            try:
                message = await publisher.publish(apartment.id, ad)
                await apartments.mark_published(
                    apartment.id,
                    chat_id=settings.telegram_group_id,
                    message_id=message.message_id,
                )
            except TelegramPublishError as exc:
                logger.error("Two-bedroom publish failed id=%s: %s", ad.lalafo_id, exc)
                failures += 1
                continue
            published += 1
            logger.info(
                "TWO_BEDROOM_PUBLISHED lalafo_id=%s message_id=%s photos=%d",
                ad.lalafo_id,
                message.message_id,
                len(ad.photo_urls),
            )
            if index < len(selected) - 1:
                await asyncio.sleep(SOURCE_PUBLISH_SPACING_SECONDS)
    finally:
        await bot.session.close()
        await engine.dispose()

    logger.info("Two-bedroom publication finished: published=%d failed=%d", published, failures)
    return 0 if failures == 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

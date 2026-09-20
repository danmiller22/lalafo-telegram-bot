from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository, as_utc
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError, is_allowed
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from scripts.scrape_publish import (
    SOURCE_ALLOWED_ROOMS,
    SOURCE_MAX_PRICE,
    SOURCE_MIN_PHOTOS,
    apartment_to_ad,
    is_substandard_structure,
    minimum_price_for_rooms,
)


logger = logging.getLogger(__name__)


def _valid(ad, settings) -> tuple[bool, str]:
    allowed, reason = is_allowed(
        ad,
        city=settings.city,
        max_price=SOURCE_MAX_PRICE,
        rooms=SOURCE_ALLOWED_ROOMS,
    )
    if not allowed:
        return False, reason
    if ad.price < max(20_000, settings.min_price, minimum_price_for_rooms(ad.rooms)):
        return False, "min_price"
    if len(ad.photo_urls) < SOURCE_MIN_PHOTOS:
        return False, "too_few_photos"
    if not ad.no_subletting:
        return False, "shared_housing"
    if is_substandard_structure(ad):
        return False, "substandard_structure"
    return True, "ok"


async def run(*, eligible_until: datetime | None = None) -> int:
    """Atomically claim and publish at most one due inventory card."""
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    inventory = InventoryRepository(sessions)
    apartments = ApartmentRepository(sessions)
    item = await inventory.claim_due(eligible_until=eligible_until)
    if item is None:
        await engine.dispose()
        return 0

    apartment = item.apartment
    is_repeat = apartment.publication_status == "published"
    stored = apartment_to_ad(apartment)
    ad = None
    try:
        async with LalafoClient(
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            proxy_url=settings.lalafo_proxy_url,
        ) as client:
            ad = await client.detail(apartment.source_url)
    except LalafoNotFound:
        await apartments.mark_inactive(apartment.id)
        await inventory.finish_item(item.id, status="skipped", error="not_found")
        await engine.dispose()
        return 0
    except (LalafoError, LalafoParseError, ValueError) as exc:
        last_seen = as_utc(apartment.last_seen_at or apartment.updated_at)
        if last_seen < datetime.now(timezone.utc) - timedelta(hours=24):
            await inventory.finish_item(
                item.id, status="skipped", error=f"stale_{type(exc).__name__}"
            )
            await engine.dispose()
            return 0
        ad = stored

    assert ad is not None
    valid, reason = _valid(ad, settings)
    if not valid:
        await inventory.finish_item(item.id, status="skipped", error=reason)
        await engine.dispose()
        return 0
    if not is_repeat and await apartments.is_duplicate(ad):
        await inventory.finish_item(item.id, status="skipped", error="duplicate")
        await engine.dispose()
        return 0

    # Keep the private phone from the discovery snapshot if the public detail
    # endpoint temporarily omits it.
    if not ad.phone:
        ad = ad.model_copy(update={"phone": stored.phone})
    apartment = await apartments.upsert_discovered(
        ad, discovery_priority=apartment.discovery_priority
    )
    bot = Bot(token=settings.require_bot_token())
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,
        max_photos=settings.max_photos_per_apartment,
    )
    try:
        message = await publisher.publish(apartment.id, ad)
        # A Telegram acknowledgement is the point of no return. Mark the queue
        # terminal first so a later DB metadata error can never resend the card.
        await inventory.finish_item(item.id, status="published")
        try:
            await apartments.mark_published(
                apartment.id,
                chat_id=settings.telegram_group_id,
                message_id=message.message_id,
            )
        except Exception:
            logger.exception(
                "Telegram card is durable but apartment metadata acknowledgement failed"
            )
            return 1
        logger.info("Published queued apartment id=%s", apartment.lalafo_id)
        return 0
    except TelegramPublishError as exc:
        logger.warning("Queued publication will retry: %s", type(exc).__name__)
        await inventory.retry_item(item.id, error=type(exc).__name__)
        return 0
    except Exception as exc:
        logger.exception("Queued publication failed safely")
        await inventory.retry_item(item.id, error=type(exc).__name__)
        return 1
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

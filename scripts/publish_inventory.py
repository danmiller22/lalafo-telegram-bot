from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository, REPOST_AFTER_HOURS, as_utc, publication_day_start
from app.promotions import REQUESTED_WINDOW_PREFIX, requested_send_allowed
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError, is_allowed
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.lalafo.exclusions import is_excluded_agency
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from app.telegram.formatting import is_supported_source
from app.listing_policy import listing_policy_eligible
from scripts.scrape_publish import (
    SOURCE_ALLOWED_ROOMS,
    SOURCE_MAX_PRICE,
    SOURCE_MIN_PHOTOS,
    apartment_to_ad,
    is_substandard_structure,
    minimum_price_for_rooms,
)


logger = logging.getLogger(__name__)
PUBLIC_DETAIL_REFRESH_SECONDS = 10.0
ITEM_PUBLISH_TIMEOUT_SECONDS = 45.0
# Bound one hosted tick so a run cannot spend many minutes walking stale
# Lalafo rows. The next minute resumes from the durable queue.
MAX_TERMINAL_SKIPS_PER_RUN = 12
STORED_FALLBACK_MAX_AGE_HOURS = 48


def _valid(ad, settings, *, allow_chat_contact=False) -> tuple[bool, str]:
    if not is_supported_source(ad):
        return False, "unsupported_source"
    if is_excluded_agency(ad):
        return False, "excluded_agency"
    allowed, reason = is_allowed(
        ad,
        city=settings.city,
        max_price=SOURCE_MAX_PRICE,
        rooms=SOURCE_ALLOWED_ROOMS,
    )
    if not allowed and not (allow_chat_contact and reason == 'phone'):
        return False, reason
    if ad.price < max(25_000, settings.min_price, minimum_price_for_rooms(ad.rooms)):
        return False, "min_price"
    if not ad.district and ad.price < 25_000:
        return False, "missing_district_below_25000"
    if len(ad.photo_urls) < SOURCE_MIN_PHOTOS:
        return False, "too_few_photos"
    if not ad.no_subletting:
        return False, "shared_housing"
    if is_substandard_structure(ad):
        return False, "substandard_structure"
    if not listing_policy_eligible(ad):
        return False, "seller_policy"
    return True, "ok"


async def _skip(inventory: InventoryRepository, item_id: int, error: str) -> None:
    await inventory.finish_item(item_id, status="skipped", error=error)
    logger.info("Skipped queued apartment reason=%s; checking the next due card", error)


async def run(
    *,
    eligible_until: datetime | None = None,
    _remaining_skips: int = MAX_TERMINAL_SKIPS_PER_RUN,
    sessions=None,
) -> int:
    """Claim due cards until one is published, retryable, or none remain."""
    settings = get_settings()
    engine = None
    if sessions is None:
        engine, sessions = create_engine_and_session(settings.database_url)
    try:
        if engine is not None:
            await init_db(engine)
        inventory = InventoryRepository(sessions)
        apartments = ApartmentRepository(sessions)
        for _ in range(max(0, _remaining_skips) + 1):
            item = await inventory.claim_due(eligible_until=eligible_until)
            if item is None:
                return 0
            try:
                # One apartment must never consume the whole hosted dispatch
                # window. Quarantine it briefly and try the next due row in
                # this same tick.
                async with asyncio.timeout(ITEM_PUBLISH_TIMEOUT_SECONDS):
                    result = await _publish_item(
                        settings, sessions, inventory, apartments, item
                    )
            except TimeoutError:
                await inventory.retry_item(item.id, error="ItemPublishTimeout")
                logger.warning(
                    "Queued apartment exceeded %.0f seconds; checking the next due card",
                    ITEM_PUBLISH_TIMEOUT_SECONDS,
                )
                continue
            except asyncio.CancelledError:
                # The hosted dispatch deadline cancels this task. Release its
                # global publication claim before the next scheduler tick.
                # retry_item preserves terminal acknowledgements.
                await inventory.retry_item(item.id, error="DispatchCancelled")
                raise
            if result is not None:
                return result
        return 0
    finally:
        if engine is not None:
            await engine.dispose()


async def _publish_item(settings, sessions, inventory, apartments, item) -> int | None:
    apartment = item.apartment
    is_requested = item.window_key.startswith(REQUESTED_WINDOW_PREFIX)
    if is_requested:
        now = datetime.now(timezone.utc)
        start = publication_day_start(now).astimezone(timezone.utc)
        if not await requested_send_allowed(
            sessions, apartment_id=apartment.id, now=now, day_start=start,
            day_end=start + timedelta(hours=21),
        ):
            return await _skip(inventory, item.id, "requested_limit_or_gap")
    is_repeat = apartment.publication_status == "published"
    stored = apartment_to_ad(apartment)
    ad = None
    stored_fallback = False
    binding = None
    if is_requested:
        from app.managed_cards import managed_bindings
        async with sessions() as session:
            binding = (await managed_bindings(session)).get(apartment.id)
    if binding:
        from app.managed_cards import ManagedCardUnavailable, refresh_managed_card
        try:
            async with LalafoClient(timeout=15, max_retries=0, proxy_url=settings.lalafo_proxy_url) as client:
                ad = await refresh_managed_card(client, binding, stored)
        except (ManagedCardUnavailable, LalafoNotFound) as exc:
            await apartments.set_availability(apartment.id, status='unavailable',
                checked_at=datetime.now(timezone.utc), reason=type(exc).__name__)
            return await _skip(inventory, item.id, 'managed_ad_unavailable')
        except (LalafoError, ValueError, TimeoutError):
            await inventory.finish_item(item.id, status='skipped', error='managed_public_check_unknown')
            return None
    elif apartment.source_url.startswith("https://t.me/"):
        source_updated = as_utc(apartment.source_updated_at or apartment.updated_at)
        if not is_requested and source_updated < datetime.now(timezone.utc) - timedelta(hours=168):
            return await _skip(inventory, item.id, "stale_telegram")
        if is_requested:
            from app.availability import AvailabilityService
            status, source_reason = await AvailabilityService(apartments, settings).check_source(apartment.source_url)
            if status == "unavailable":
                await apartments.set_availability(apartment.id, status=status,
                    checked_at=datetime.now(timezone.utc), reason=source_reason)
                return await _skip(inventory, item.id, source_reason)
            if status != "active":
                await inventory.retry_item(item.id, error=source_reason)
                return None
        ad = stored
    else:
        try:
            async with asyncio.timeout(PUBLIC_DETAIL_REFRESH_SECONDS):
                async with LalafoClient(
                    timeout=settings.http_timeout_seconds,
                    max_retries=settings.http_max_retries,
                    proxy_url=settings.lalafo_proxy_url,
                ) as client:
                    ad = await client.detail(apartment.source_url)
        except LalafoNotFound:
            if is_requested:
                await apartments.set_availability(apartment.id, status="unavailable",
                    checked_at=datetime.now(timezone.utc), reason="source_removed")
            else:
                await apartments.mark_inactive(apartment.id)
            return await _skip(inventory, item.id, "not_found")
        except (LalafoError, LalafoParseError, ValueError, TimeoutError) as exc:
            last_seen = as_utc(apartment.last_seen_at or apartment.updated_at)
            if last_seen >= datetime.now(timezone.utc) - timedelta(
                hours=STORED_FALLBACK_MAX_AGE_HOURS
            ):
                ad = stored
                stored_fallback = True
            else:
                return await _skip(inventory, item.id, f"stale_{type(exc).__name__}")

    assert ad is not None
    valid, reason = _valid(ad, settings, allow_chat_contact=bool(binding))
    # Hand-picked cards may legitimately have a different budget/room count;
    # source, whole-apartment, structure and lodging protections still apply.
    if is_requested and reason in {
        "rooms", "min_price", "max_price", "price", "seller_policy"
    }:
        valid = (is_supported_source(ad) and not is_excluded_agency(ad)
                 and bool(ad.photo_urls) and bool(ad.phone)
                 and ad.price > 0 and ad.no_subletting and not is_substandard_structure(ad))
    if not valid:
        return await _skip(inventory, item.id, reason)
    if not is_requested and await apartments.is_duplicate(
        ad,
        repost_before=(datetime.now(timezone.utc) - timedelta(hours=REPOST_AFTER_HOURS))
        if is_repeat else None,
    ):
        return await _skip(inventory, item.id, "duplicate")

    # Keep the private phone from the discovery snapshot if the public detail
    # endpoint temporarily omits it.
    if not ad.phone:
        ad = ad.model_copy(update={"phone": stored.phone})
    if not binding and not (is_requested and stored_fallback):
        apartment = await apartments.upsert_discovered(
            ad, discovery_priority=apartment.discovery_priority
        )
    if is_requested:
        now = datetime.now(timezone.utc)
        start = publication_day_start(now).astimezone(timezone.utc)
        if not await requested_send_allowed(
            sessions, apartment_id=apartment.id, now=now, day_start=start,
            day_end=start + timedelta(hours=21),
        ):
            return await _skip(inventory, item.id, "requested_limit_or_gap")
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
                acknowledged_at=getattr(message, "date", None),
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
        if binding:
            await inventory.finish_item(item.id, status='skipped', error=type(exc).__name__)
        else:
            await inventory.retry_item(item.id, error=type(exc).__name__)
        # A retryable card must not consume the whole scheduler tick. Its
        # claim has already been safely rescheduled, so continue with another
        # due apartment from the durable queue.
        return None
    except Exception as exc:
        logger.exception("Queued publication failed safely")
        if binding:
            await inventory.finish_item(item.id, status='skipped', error=type(exc).__name__)
        else:
            await inventory.retry_item(item.id, error=type(exc).__name__)
        return None
    finally:
        await bot.session.close()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

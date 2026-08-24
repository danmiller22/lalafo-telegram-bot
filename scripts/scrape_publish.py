from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError, is_allowed
from app.lalafo.phone import mask_phone
from app.state import PostedState, ad_fingerprint
from app.telegram.formatting import format_apartment

logger = logging.getLogger(__name__)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = PostedState.load(settings.posted_state_path)
    limit = settings.effective_post_limit
    candidates = []
    total_found = 0
    page_number = 1

    engine = None
    apartments = None
    token = ""
    callback_secret = ""
    if not settings.dry_run:
        from app.database import create_engine_and_session, init_db
        from app.payments.repository import ApartmentRepository

        try:
            token = settings.require_bot_token()
            callback_secret = settings.require_callback_secret()
        except RuntimeError as exc:
            logger.error("Production configuration is incomplete: %s", exc)
            return 2
        engine, sessions = create_engine_and_session(settings.database_url)
        try:
            await init_db(engine)
        except Exception as exc:
            logger.error("Database initialization failed safely: %s", type(exc).__name__)
            await engine.dispose()
            return 2
        apartments = ApartmentRepository(sessions)

    async with LalafoClient(
        timeout=settings.http_timeout_seconds, max_retries=settings.http_max_retries
    ) as client:
        while len(candidates) < limit:
            try:
                page = await client.search(settings.lalafo_search_url, page=page_number)
            except (LalafoError, LalafoParseError) as exc:
                logger.error("Search failed safely: %s", exc)
                if engine is not None:
                    await engine.dispose()
                return 2
            if page_number == 1:
                total_found = page.total
                logger.info("Lalafo search found %d advertisements", total_found)
            if not page.items:
                break
            page.items.sort(
                key=lambda item: item.updated_at.timestamp() if item.updated_at else 0,
                reverse=True,
            )
            for search_ad in page.items:
                if len(candidates) >= limit:
                    break
                if state.contains(search_ad.lalafo_id):
                    continue
                if search_ad.currency and search_ad.currency.upper() != "KGS":
                    continue
                if search_ad.price and search_ad.price > settings.max_price:
                    continue
                if settings.only_with_photos and not search_ad.photo_urls:
                    continue
                try:
                    ad = await client.detail(search_ad.detail_url)
                except LalafoNotFound:
                    logger.info("Skipping unavailable ad id=%s", search_ad.lalafo_id)
                    continue
                except (LalafoError, LalafoParseError, ValueError) as exc:
                    logger.warning(
                        "Skipping broken ad id=%s error=%s",
                        search_ad.lalafo_id,
                        type(exc).__name__,
                    )
                    continue
                allowed, reason = is_allowed(
                    ad,
                    city=settings.city,
                    max_price=settings.max_price,
                    rooms=settings.allowed_rooms,
                )
                if not allowed:
                    logger.info("Skipping ad id=%s reason=%s", ad.lalafo_id, reason)
                    continue
                if not settings.allow_no_district and not ad.district:
                    logger.info("Skipping ad id=%s reason=district", ad.lalafo_id)
                    continue
                if not settings.allow_no_deposit and ad.deposit is None:
                    logger.info("Skipping ad id=%s reason=deposit", ad.lalafo_id)
                    continue
                if state.contains(ad.lalafo_id, ad_fingerprint(ad)):
                    continue
                if apartments is not None and await apartments.is_duplicate(ad):
                    logger.info("Skipping DB duplicate id=%s", ad.lalafo_id)
                    continue
                candidates.append(ad)
            if page_number >= page.page_count:
                break
            page_number += 1

    logger.info("Eligible new apartments selected: %d", len(candidates))
    for ad in candidates:
        logger.info(
            "DRY candidate id=%s rooms=%s city=%s district=%s price=%s deposit=%s photos=%s phone=%s",
            ad.lalafo_id,
            ad.rooms,
            ad.city,
            ad.district or "-",
            ad.price,
            ad.deposit if ad.deposit is not None else "-",
            len(ad.photo_urls),
            mask_phone(ad.phone),
        )
        logger.info("Telegram preview:\n%s", format_apartment(ad))

    if settings.dry_run:
        logger.info("DRY_RUN enabled: Telegram, database and state were not changed")
        return 0
    if not candidates:
        if engine is not None:
            await engine.dispose()
        return 0

    # Keep DRY_RUN lightweight enough for free 512 MB services. These modules
    # are needed only when a real Telegram/database publication is requested.
    from aiogram import Bot

    from app.security import TokenSigner
    from app.telegram.publisher import TelegramPublishError, TelegramPublisher

    assert engine is not None
    assert apartments is not None
    signer = TokenSigner(callback_secret)
    bot = Bot(token=token)
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=signer,
        max_photos=settings.max_photos_per_apartment,
    )
    published = 0
    try:
        for ad in candidates:
            if await apartments.is_duplicate(ad):
                logger.info("Skipping DB duplicate id=%s", ad.lalafo_id)
                continue
            apartment = await apartments.upsert_discovered(ad)
            try:
                message = await publisher.publish(apartment.id, ad)
            except TelegramPublishError as exc:
                logger.error("Publish failed for id=%s: %s", ad.lalafo_id, exc)
                continue
            await apartments.mark_published(
                apartment.id, chat_id=settings.telegram_group_id, message_id=message.message_id
            )
            state.add(ad, telegram_message_id=message.message_id)
            published += 1
    finally:
        await bot.session.close()
        await engine.dispose()
    if published:
        state.prune(settings.state_retention_days)
        state.save()
    logger.info("Published apartments: %d", published)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

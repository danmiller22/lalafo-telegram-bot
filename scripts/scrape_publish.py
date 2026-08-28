from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging
import math
import re

from app.config import DEFAULT_SEARCH_URL, get_settings
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.models import LalafoAd, SearchAd
from app.lalafo.parser import LalafoParseError, is_allowed
from app.lalafo.phone import mask_phone
from app.lalafo.subletting import halve_subletting_candidates
from app.state import PostedState, ad_fingerprint
from app.telegram.formatting import format_apartment

logger = logging.getLogger(__name__)

PREFERRED_DISTRICT_TERMS = (
    "центр",
    "золотой квадрат",
    "площадь ала-тоо",
    "эркиндик",
    "филармони",
    "цум",
    "гум",
    "восток-5",
    "восток 5",
    "дордой плаза",
    "dordoi plaza",
    "бишкек парк",
    "караван",
    "площадь",
    "ошский рынок",
    "ош базар",
    "молодая гвардия",
    "аламедин-1",
    "аламедин 1",
    "азия молл",
    "вефа",
    "бгу",
    "карпинка",
    "пишпек",
    "западный автовокзал",
    "политех",
)
CENTRAL_DISTRICT_TERMS = (
    "центр",
    "золотой квадрат",
    "площадь ала-тоо",
    "эркиндик",
    "филармони",
    "цум",
    "гум",
    "дордой плаза",
    "бишкек парк",
    "караван",
    "азия молл",
    "вефа",
    "бгу",
    "карпинка",
    "пишпек",
    "западный автовокзал",
    "политех",
)
SOURCE_MIN_PRICE = 18_000
SOURCE_MAX_PRICE = 35_000
SOURCE_ALLOWED_ROOMS = ("studio", "1")
SOURCE_MAX_POSTS_PER_RUN = 50
SOURCE_MAX_SEARCH_PAGES = 15
PREFERRED_BATCH_SHARE = 0.70
MAX_CANDIDATE_POOL = 120


async def fetch_detail_batch(
    search_ads: list[SearchAd],
    clients: list[LalafoClient],
) -> list[tuple[SearchAd, LalafoAd | None]]:
    """Fetch details concurrently without sharing a rotating HTTP client."""
    chunks = [search_ads[index :: len(clients)] for index in range(len(clients))]

    async def worker(client: LalafoClient, items):
        results = []
        for search_ad in items:
            try:
                ad = await client.detail(search_ad.detail_url)
            except LalafoNotFound:
                logger.info("Skipping unavailable ad id=%s", search_ad.lalafo_id)
                ad = None
            except (LalafoError, LalafoParseError, ValueError) as exc:
                logger.warning(
                    "Skipping broken ad id=%s error=%s",
                    search_ad.lalafo_id,
                    type(exc).__name__,
                )
                ad = None
            results.append((search_ad, ad))
        return results

    batches = await asyncio.gather(
        *(worker(client, chunk) for client, chunk in zip(clients, chunks) if chunk)
    )
    by_id = {
        search_ad.lalafo_id: (search_ad, ad)
        for batch in batches
        for search_ad, ad in batch
    }
    return [by_id[search_ad.lalafo_id] for search_ad in search_ads]


def is_preferred_district(district: str | None) -> bool:
    normalized = (district or "").casefold().replace("ё", "е")
    return any(term in normalized for term in PREFERRED_DISTRICT_TERMS) or bool(
        re.search(r"(?<!\d)[567]\s*мкр", normalized)
    )


def is_central_district(district: str | None) -> bool:
    normalized = (district or "").casefold().replace("ё", "е")
    return any(term in normalized for term in CENTRAL_DISTRICT_TERMS)


def candidate_quality(ad: LalafoAd) -> tuple[int, bool, bool, bool, int, int, bool, float]:
    """Put cheap central apartments first, then other requested-area bargains."""
    updated_at = ad.source_updated_at.timestamp() if ad.source_updated_at else 0
    central = is_central_district(ad.district)
    preferred = is_preferred_district(ad.district)
    affordable = ad.price <= 32_000
    very_affordable = ad.price <= 27_000
    priority_score = (
        (5 if central else 0)
        + (3 if preferred else 0)
        + (3 if affordable else 0)
        + (1 if very_affordable else 0)
    )
    return (
        priority_score,
        central,
        affordable,
        len(ad.photo_urls) >= 5,
        -ad.price,
        len(ad.photo_urls),
        bool(ad.district),
        updated_at,
    )


def select_publish_batch(candidates: list[LalafoAd], limit: int) -> list[LalafoAd]:
    """Build a batch targeting 70% requested districts when supply allows it."""
    if limit <= 0 or not candidates:
        return []
    preferred = sorted(
        (ad for ad in candidates if is_preferred_district(ad.district)),
        key=candidate_quality,
        reverse=True,
    )
    other = sorted(
        (ad for ad in candidates if not is_preferred_district(ad.district)),
        key=candidate_quality,
        reverse=True,
    )
    total = min(limit, len(candidates))
    preferred_target = min(len(preferred), math.ceil(total * PREFERRED_BATCH_SHARE))
    selected = preferred[:preferred_target]
    selected.extend(other[: total - len(selected)])
    if len(selected) < total:
        selected.extend(preferred[preferred_target : preferred_target + total - len(selected)])
    return sorted(selected, key=candidate_quality, reverse=True)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = PostedState.load(settings.posted_state_path)
    limit = min(settings.effective_post_limit, SOURCE_MAX_POSTS_PER_RUN)
    candidate_pool_limit = max(limit, min(limit * 3, MAX_CANDIDATE_POOL))
    candidates = []
    candidate_phones: set[str] = set()
    repost_candidate_ids: set[int] = set()

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

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
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
        page_number = 1
        while len(candidates) < candidate_pool_limit:
            try:
                page = await client.search(DEFAULT_SEARCH_URL, page=page_number)
            except (LalafoError, LalafoParseError) as exc:
                logger.error("Search failed safely: %s", exc)
                if engine is not None:
                    await engine.dispose()
                return 2
            if page_number == 1:
                logger.info("Lalafo source search found %d advertisements", page.total)
            if not page.items:
                break
            page.items.sort(
                key=lambda item: item.updated_at.timestamp() if item.updated_at else 0,
                reverse=True,
            )
            published_ids = (
                await apartments.published_lalafo_ids(
                    [item.lalafo_id for item in page.items]
                )
                if apartments is not None
                else set()
            )
            repostable_ids = (
                await apartments.repostable_lalafo_ids(
                    [item.lalafo_id for item in page.items],
                    after_hours=settings.repost_after_hours,
                )
                if apartments is not None
                else set()
            )
            detail_search_ads = []
            for search_ad in page.items:
                is_repost = search_ad.lalafo_id in repostable_ids
                if state.contains(search_ad.lalafo_id) and not is_repost:
                    continue
                if search_ad.lalafo_id in published_ids and not is_repost:
                    continue
                if search_ad.currency and search_ad.currency.upper() != "KGS":
                    continue
                if search_ad.price and not (
                    max(settings.min_price, SOURCE_MIN_PRICE)
                    <= search_ad.price
                    <= SOURCE_MAX_PRICE
                ):
                    continue
                if settings.only_with_photos and not search_ad.photo_urls:
                    continue
                detail_search_ads.append(search_ad)

            details = await fetch_detail_batch(detail_search_ads, detail_clients)
            parsed_ads = [ad for _, ad in details if ad is not None]
            duplicate_ids = (
                await apartments.duplicate_candidate_ids(parsed_ads)
                if apartments is not None
                else set()
            )
            for search_ad, ad in details:
                if len(candidates) >= candidate_pool_limit:
                    break
                if ad is None:
                    continue
                is_repost = search_ad.lalafo_id in repostable_ids
                allowed, reason = is_allowed(
                    ad,
                    city=settings.city,
                    max_price=SOURCE_MAX_PRICE,
                    rooms=SOURCE_ALLOWED_ROOMS,
                )
                if not allowed:
                    logger.info("Skipping ad id=%s reason=%s", ad.lalafo_id, reason)
                    continue
                if ad.price < max(settings.min_price, SOURCE_MIN_PRICE):
                    logger.info("Skipping ad id=%s reason=min_price", ad.lalafo_id)
                    continue
                if not settings.allow_no_district and not ad.district:
                    logger.info("Skipping ad id=%s reason=district", ad.lalafo_id)
                    continue
                if not settings.allow_no_deposit and ad.deposit is None:
                    logger.info("Skipping ad id=%s reason=deposit", ad.lalafo_id)
                    continue
                if state.contains(ad.lalafo_id, ad_fingerprint(ad)) and not is_repost:
                    continue
                if ad.lalafo_id in duplicate_ids and not is_repost:
                    logger.info("Skipping DB duplicate id=%s", ad.lalafo_id)
                    continue
                if ad.phone in candidate_phones:
                    logger.info("Skipping repeated contact id=%s", ad.lalafo_id)
                    continue
                candidates.append(ad)
                candidate_phones.add(ad.phone)
                if is_repost:
                    repost_candidate_ids.add(ad.lalafo_id)
            if page_number >= min(
                page.page_count,
                settings.max_search_pages,
                SOURCE_MAX_SEARCH_PAGES,
            ):
                break
            page_number += 1

    shared_before = sum(not ad.no_subletting for ad in candidates)
    candidates = halve_subletting_candidates(candidates)
    shared_after = sum(not ad.no_subletting for ad in candidates)
    logger.info(
        "Subletting candidate reduction: %d -> %d (target: 50%% fewer)",
        shared_before,
        shared_after,
    )
    candidates = select_publish_batch(candidates, limit)
    repost_candidate_ids.intersection_update(ad.lalafo_id for ad in candidates)
    preferred_count = sum(is_preferred_district(ad.district) for ad in candidates)
    preferred_percent = round(preferred_count * 100 / len(candidates)) if candidates else 0
    logger.info(
        "Preferred-district share: %d/%d (%d%%), target=%d%%",
        preferred_count,
        len(candidates),
        preferred_percent,
        round(PREFERRED_BATCH_SHARE * 100),
    )
    logger.info("Eligible photo-prioritized apartments selected: %d", len(candidates))
    for ad in candidates:
        logger.info(
            "Candidate id=%s rooms=%s city=%s district=%s price=%s deposit=%s photos=%s phone=%s",
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
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_url,
        max_photos=settings.max_photos_per_apartment,
    )
    published = 0
    publish_failures = 0
    publish_semaphore = asyncio.Semaphore(
        max(1, min(5, settings.apartment_publish_concurrency))
    )

    async def publish_one(ad: LalafoAd) -> tuple[LalafoAd, int | None, bool]:
        async with publish_semaphore:
            if (
                await apartments.is_duplicate(ad)
                and ad.lalafo_id not in repost_candidate_ids
            ):
                logger.info("Skipping DB duplicate id=%s", ad.lalafo_id)
                return ad, None, False
            apartment = await apartments.upsert_discovered(ad)
            try:
                message = await publisher.publish(apartment.id, ad)
            except TelegramPublishError as exc:
                logger.error("Publish failed for id=%s: %s", ad.lalafo_id, exc)
                return ad, None, True
            for attempt in range(5):
                try:
                    await apartments.mark_published(
                        apartment.id,
                        chat_id=settings.telegram_group_id,
                        message_id=message.message_id,
                    )
                    return ad, message.message_id, False
                except Exception:
                    if attempt == 4:
                        logger.exception(
                            "Database acknowledgement permanently failed for id=%s",
                            ad.lalafo_id,
                        )
                        return ad, None, True
                    wait_seconds = 2**attempt
                    logger.warning(
                        "Database acknowledgement failed for id=%s; retrying in %ds",
                        ad.lalafo_id,
                        wait_seconds,
                    )
                    await asyncio.sleep(wait_seconds)
        return ad, None, True

    try:
        results = await asyncio.gather(*(publish_one(ad) for ad in candidates))
        for ad, message_id, failed in results:
            if failed:
                publish_failures += 1
            elif message_id is not None:
                state.add(ad, telegram_message_id=message_id)
                published += 1
    finally:
        await bot.session.close()
        await engine.dispose()
    if published:
        state.prune(settings.state_retention_days)
        state.save()
    logger.info("Published apartments: %d", published)
    if candidates and published == 0 and publish_failures:
        logger.error(
            "All %d Telegram publications failed; requesting a full-cycle retry",
            publish_failures,
        )
        return 2
    if publish_failures:
        logger.warning(
            "Partial Telegram delivery: published=%d failed=%d; failed cards remain retryable",
            published,
            publish_failures,
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

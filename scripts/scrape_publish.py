from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, time, timezone
import logging
import math
import random
import re
from collections.abc import Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import ADDITIONAL_SEARCH_URLS, DEFAULT_SEARCH_URL, get_settings
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.exclusions import is_permanently_excluded
from app.lalafo.models import LalafoAd, SearchAd
from app.lalafo.parser import LalafoParseError, is_allowed
from app.lalafo.phone import mask_phone
from app.models import Apartment
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
# The fallback search includes owners and real-estate agents; detail-level
# checks still remove shared housing and all public cards omit offerer type.
SOURCE_MIN_PRICE = 10_000
SOURCE_MAX_PRICE = 40_000
SOURCE_ALLOWED_ROOMS = ("1", "studio", "2")
SOURCE_MIN_PHOTOS = 2
SOURCE_MAX_POSTS_PER_RUN = 18
SOURCE_PUBLISH_SPACING_SECONDS = 150
SOURCE_MAX_SEARCH_PAGES = 36
# Published apartments are terminal: every cycle must use fresh inventory.
SOURCE_REPOST_AFTER_HOURS = None
MAX_REPOSTS_PER_RUN = 0
CENTRAL_BATCH_SHARE = 0.50
OWNER_OTHER_BATCH_SHARE = 0.50
MAX_CANDIDATE_POOL = 300
# Keep nearly half of the discovery pool available for agents. This fallback
# grows the catalogue after unique owner listings have been exhausted.
REALTOR_CANDIDATE_RESERVE_SHARE = 0.45
# Two-bedroom cards are mixed into the normal stream instead of being sent as
# a separate burst. Two per regular cycle reaches at most twenty per Bishkek day.
TWO_BEDROOM_MIN_PRICE = 20_000
TWO_BEDROOM_MAX_PRICE = 40_000
TWO_BEDROOM_DAILY_LIMIT = 20
TWO_BEDROOM_MAX_PER_RUN = 2
MANAGED_PROFILE_MAX_PER_RUN = 1
BISHKEK = ZoneInfo("Asia/Bishkek")
# Manually approved cards remain eligible once, using their original,
# phone-backed database records rather than phone-hidden public reposts.
CURATED_ROTATION_SPECS = (
    ("Моссовет", 20_000),
)
CURATED_ROTATION_LALAFO_IDS = (
    115333471,
    112925333,
    114091573,
    116107608,
    116136417,
    115936987,
    116040769,
    116159856,
    114533207,
    116120466,
)
PRIORITY_AD_SPECS = (
    (
        "https://lalafo.kg/bishkek/ads/1-komnata-1000-melocej-"
        "agentstvo-nedvizimosti-bez-zivotnyh-ot-1-mesaca-ot-3-mesacev-"
        "ot-6-mesacev-s-mebelu-casticno-id-115806919",
        "1000 мелочей — Дордой Плаза ТЦ",
    ),
    (
        "https://lalafo.kg/bishkek/ads/"
        "sdaetsa-svetlaa-studia1-komnatnaa-id-115746322",
        "Карпинка — Восток-5",
    ),
    (
        "https://lalafo.kg/bishkek/ads/"
        "sdau-kvartiru-vostok5-po-cuj-s-mebelu-id-116273232",
        "Восток-5",
    ),
    (
        "https://lalafo.kg/bishkek/ads/"
        "sdau-kvartiru-kievskaa-kalyk-akieva-id-114324774",
        "Филармония",
    ),
)


def apartment_to_ad(apartment) -> LalafoAd:
    """Build the immutable publication payload stored for an apartment."""
    return LalafoAd(
        lalafo_id=apartment.lalafo_id,
        source_url=apartment.source_url,
        phone=apartment.phone,
        price=apartment.price,
        currency="KGS",
        rooms=apartment.rooms,
        district=apartment.district,
        city=apartment.city,
        deposit=apartment.deposit,
        photo_urls=list(apartment.photo_urls),
        category_id=2044,
        no_subletting=apartment.no_subletting,
        owner_listing=False,
        source_updated_at=apartment.source_updated_at,
    )


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


def deduplicate_candidates(candidates: list[LalafoAd]) -> list[LalafoAd]:
    """Keep exactly one best representation of every Lalafo advertisement.

    Lalafo can repeat an item across neighbouring search pages.  Reposts are
    allowed across separate hourly cycles, but the same Lalafo ID must
    never occupy two slots in one Telegram batch.
    """
    unique: dict[int, LalafoAd] = {}
    for ad in candidates:
        if is_permanently_excluded(ad.lalafo_id):
            logger.info("Skipping permanently excluded ad id=%s", ad.lalafo_id)
            continue
        current = unique.get(ad.lalafo_id)
        if current is None or candidate_quality(ad) > candidate_quality(current):
            unique[ad.lalafo_id] = ad
    return list(unique.values())


def minimum_price_for_rooms(rooms: str) -> int:
    return TWO_BEDROOM_MIN_PRICE if rooms == "2" else SOURCE_MIN_PRICE


def is_substandard_structure(ad: LalafoAd) -> bool:
    """Reject containers, temporary housing and apartment-like barracks."""
    text = re.sub(
        r"[^\w]+",
        " ",
        f"{ad.source_title} {ad.source_description}".casefold().replace("ё", "е"),
    )
    blocked_terms = (
        "контейнер",
        "времянка",
        "вагончик",
        "барак",
        "барачного типа",
        "модульный дом",
        "общежитие",
    )
    if any(term in text for term in blocked_terms):
        return True

    params = {
        str(item.get("name") or "").casefold(): str(item.get("value") or "").strip()
        for item in ad.source_params
    }
    # Lalafo listings marked as both floor 1 and a one-floor building are
    # overwhelmingly temporary/private-yard units rather than apartments.
    return params.get("этаж") == "1" and params.get("количество этажей") == "1"


def managed_source_to_ad(apartment, managed_ad: LalafoAd) -> LalafoAd:
    """Keep the original photos/contact while mirroring our profile price/area."""
    return apartment_to_ad(apartment).model_copy(
        update={
            "price": managed_ad.price,
            "district": managed_ad.district,
        }
    )


def choose_managed_profile_cards(
    candidates: list[LalafoAd],
    *,
    limit: int = MANAGED_PROFILE_MAX_PER_RUN,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[LalafoAd]:
    """Choose a small random slice so managed-profile originals never arrive in a burst."""
    unique = deduplicate_candidates(candidates)
    if limit <= 0 or not unique:
        return []
    chooser = rng or random.SystemRandom()
    return chooser.sample(unique, k=min(limit, len(unique)))


def insert_randomly(
    cards: list[LalafoAd],
    additions: list[LalafoAd],
    *,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[LalafoAd]:
    """Place isolated profile cards at unpredictable positions in a mixed batch."""
    result = list(cards)
    chooser = rng or random.SystemRandom()
    for card in additions:
        result.insert(chooser.randrange(len(result) + 1), card)
    return result


def source_candidate_targets(
    pool_limit: int,
    source_count: int,
    batch_limit: int,
) -> list[int]:
    """Reserve a large final slice for the realtor-only fallback source."""
    if source_count <= 1:
        return [pool_limit]
    realtor_start = max(
        batch_limit,
        math.floor(pool_limit * (1 - REALTOR_CANDIDATE_RESERVE_SHARE)),
    )
    owner_source_count = source_count - 1
    targets = [
        max(batch_limit, math.floor(realtor_start * (index + 1) / owner_source_count))
        for index in range(owner_source_count)
    ]
    return [min(pool_limit, target) for target in targets] + [pool_limit]


def mix_room_types(candidates: list[LalafoAd]) -> list[LalafoAd]:
    """Interleave studios, two-bedroom and one-bedroom cards without bursts."""
    queues = {
        room: [ad for ad in candidates if ad.rooms == room]
        for room in ("studio", "2", "1")
    }
    mixed: list[LalafoAd] = []
    while any(queues.values()):
        for room in ("studio", "2", "1"):
            if queues[room]:
                mixed.append(queues[room].pop(0))
    mixed.extend(ad for ad in candidates if ad.rooms not in queues)
    return mixed


async def published_two_bedrooms_today(
    sessions, *, now: datetime | None = None
) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(BISHKEK)
    start = datetime.combine(current.date(), time.min, tzinfo=BISHKEK).astimezone(
        timezone.utc
    )
    end = datetime.combine(current.date(), time.max, tzinfo=BISHKEK).astimezone(
        timezone.utc
    )
    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Apartment)
                .where(
                    Apartment.publication_status == "published",
                    Apartment.rooms == "2",
                    Apartment.price >= TWO_BEDROOM_MIN_PRICE,
                    Apartment.price <= TWO_BEDROOM_MAX_PRICE,
                    Apartment.published_at.is_not(None),
                    Apartment.published_at >= start,
                    Apartment.published_at <= end,
                )
            )
            or 0
        )


def eligible_curated_apartments(
    apartments,
    published_ids: set[int],
    repostable_ids: set[int],
):
    """Exclude curated cards that were published less than six hours ago."""
    return [
        apartment
        for apartment in apartments
        if apartment.lalafo_id not in published_ids
        or apartment.lalafo_id in repostable_ids
    ]


def select_publish_batch(
    candidates: list[LalafoAd],
    limit: int,
    *,
    rank_key: Callable[[LalafoAd], tuple] = candidate_quality,
) -> list[LalafoAd]:
    """Build a 50/50 batch from central and other-district listings."""
    if limit <= 0 or not candidates:
        return []
    candidates = deduplicate_candidates(candidates)
    if not candidates:
        return []
    central = sorted(
        (ad for ad in candidates if is_central_district(ad.district)),
        key=rank_key,
        reverse=True,
    )
    owner_other = sorted(
        (ad for ad in candidates if not is_central_district(ad.district)),
        key=rank_key,
        reverse=True,
    )
    total = min(limit, len(candidates))
    central_target = min(len(central), math.ceil(total * CENTRAL_BATCH_SHARE))
    owner_target = min(len(owner_other), total - central_target)
    selected = central[:central_target]
    selected.extend(owner_other[:owner_target])

    # If either half is temporarily short, keep the run full from the other.
    if len(selected) < total:
        selected_ids = {ad.lalafo_id for ad in selected}
        owner_remainder = [
            ad for ad in owner_other if ad.lalafo_id not in selected_ids
        ]
        selected.extend(owner_remainder[: total - len(selected)])
    if len(selected) < total:
        selected_ids = {ad.lalafo_id for ad in selected}
        central_remainder = [
            ad for ad in central if ad.lalafo_id not in selected_ids
        ]
        selected.extend(central_remainder[: total - len(selected)])
    if len(selected) < total:
        selected_ids = {ad.lalafo_id for ad in selected}
        remaining = sorted(
            (ad for ad in candidates if ad.lalafo_id not in selected_ids),
            key=rank_key,
            reverse=True,
        )
        selected.extend(remaining[: total - len(selected)])
    return sorted(selected, key=rank_key, reverse=True)


def select_publish_batch_with_reposts(
    candidates: list[LalafoAd],
    repost_last_published_at: Mapping[int, datetime],
    limit: int,
) -> list[LalafoAd]:
    """Use fresh cards first, then fill gaps with the oldest eligible reposts."""
    if limit <= 0 or not candidates:
        return []
    candidates = deduplicate_candidates(candidates)
    repost_candidate_ids = set(repost_last_published_at)
    fresh = [ad for ad in candidates if ad.lalafo_id not in repost_candidate_ids]
    repeats = [ad for ad in candidates if ad.lalafo_id in repost_candidate_ids]
    selected = select_publish_batch(fresh, min(len(fresh), limit))
    repeat_limit = min(
        MAX_REPOSTS_PER_RUN,
        len(repeats),
        max(0, limit - len(selected)),
    )
    if repeat_limit:
        far_future = datetime.max.replace(tzinfo=timezone.utc)

        def oldest_first(ad: LalafoAd) -> tuple:
            published_at = repost_last_published_at.get(ad.lalafo_id, far_future)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            return (-published_at.timestamp(), *candidate_quality(ad))

        selected.extend(
            select_publish_batch(repeats, repeat_limit, rank_key=oldest_first)
        )
    return sorted(selected, key=candidate_quality, reverse=True)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = PostedState.load(settings.posted_state_path)
    # Product-level source limits deliberately ignore stale cloud overrides.
    # Test mode remains one-card-only, while production always has room for
    # the requested expanded mixed batch.
    limit = 1 if settings.test_mode else SOURCE_MAX_POSTS_PER_RUN
    # The unfiltered source is large. Inspect several pages so central bargains
    # can outrank nearer but weaker results from the first page.
    candidate_pool_limit = max(limit, min(limit * 15, MAX_CANDIDATE_POOL))
    candidates = []
    candidate_ids: set[int] = set()
    curated_ids: set[int] = set()
    managed_profile_ids: set[int] = set()
    managed_sources = []
    repost_candidate_ids: set[int] = set()
    repost_last_published_at: dict[int, datetime] = {}
    two_bedrooms_published_today = 0
    direct_priority_count = 0

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
        two_bedrooms_published_today = await published_two_bedrooms_today(sessions)

        curated_apartments = await apartments.curated_rotation_apartments(
            CURATED_ROTATION_SPECS
        )
        curated_apartments.extend(
            await apartments.curated_rotation_apartments_by_ids(
                CURATED_ROTATION_LALAFO_IDS
            )
        )
        managed_sources = await apartments.managed_lalafo_sources()
        curated_apartments = list(
            {item.lalafo_id: item for item in curated_apartments}.values()
        )
        curated_apartments = [
            item
            for item in curated_apartments
            if not is_permanently_excluded(item.lalafo_id)
        ]
        available_curated_count = len(curated_apartments)
        all_curated_ids = [item.lalafo_id for item in curated_apartments]
        published_curated_ids = await apartments.published_lalafo_ids(
            all_curated_ids
        )
        curated_apartments = eligible_curated_apartments(
            curated_apartments,
            published_curated_ids,
            set(),
        )
        candidates.extend(apartment_to_ad(item) for item in curated_apartments)
        curated_ids = {item.lalafo_id for item in curated_apartments}
        candidate_ids.update(curated_ids)
        missing_curated = len(CURATED_ROTATION_SPECS) - available_curated_count
        if missing_curated:
            logger.warning(
                "Curated rotation is missing %d/%d source apartments; "
                "the regular batch will continue safely",
                missing_curated,
                len(CURATED_ROTATION_SPECS),
            )
        else:
            logger.info(
                "Curated hourly rotation loaded: %s",
                ", ".join(str(item.lalafo_id) for item in curated_apartments),
            )

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
        # Ads on our own Lalafo profile intentionally hide/replace the source
        # contact. Restore the original card, but mirror the live profile price
        # and district exactly. An unavailable managed ad is no longer current
        # and is skipped. At most one is selected later, preventing a burst.
        for managed_source in managed_sources:
            source = managed_source.apartment
            if is_permanently_excluded(source.lalafo_id):
                continue
            if state.contains(source.lalafo_id):
                continue
            if apartments is not None:
                published_source_ids = await apartments.published_lalafo_ids(
                    [source.lalafo_id]
                )
                if source.lalafo_id in published_source_ids:
                    continue
            managed_url = managed_source.managed_lalafo_ad_url
            if not managed_url and managed_source.managed_lalafo_ad_id:
                managed_url = (
                    "https://lalafo.kg/bishkek/ads/"
                    f"managed-id-{managed_source.managed_lalafo_ad_id}"
                )
            if not managed_url:
                continue
            try:
                managed_ad = await client.detail(managed_url)
            except (LalafoError, LalafoParseError, ValueError) as exc:
                logger.info(
                    "Skipping inactive managed profile ad source_id=%s error=%s",
                    source.lalafo_id,
                    type(exc).__name__,
                )
                continue
            merged_ad = managed_source_to_ad(source, managed_ad)
            allowed, reason = is_allowed(
                merged_ad,
                city=settings.city,
                max_price=SOURCE_MAX_PRICE,
                rooms=SOURCE_ALLOWED_ROOMS,
            )
            if not allowed or merged_ad.price < max(
                settings.min_price,
                minimum_price_for_rooms(merged_ad.rooms),
            ):
                logger.info(
                    "Skipping managed profile source id=%s reason=%s",
                    source.lalafo_id,
                    reason if not allowed else "min_price",
                )
                continue
            if len(merged_ad.photo_urls) < SOURCE_MIN_PHOTOS:
                continue
            if not merged_ad.no_subletting or is_substandard_structure(merged_ad):
                continue
            duplicate_ids = (
                await apartments.duplicate_candidate_ids([merged_ad])
                if apartments is not None
                else set()
            )
            if source.lalafo_id in duplicate_ids:
                continue
            candidates.append(merged_ad)
            candidate_ids.add(source.lalafo_id)
            curated_ids.add(source.lalafo_id)
            managed_profile_ids.add(source.lalafo_id)
        logger.info(
            "Active managed-profile originals eligible for random rotation: %d",
            len(managed_profile_ids),
        )

        # Operator-supplied cards are fetched directly so they do not depend on
        # their position in Lalafo search results. Normal published-ID and
        # fingerprint/phone checks still make every card strictly one-time.
        for priority_url, district_label in PRIORITY_AD_SPECS:
            match = re.search(r"-id-(\d+)(?:[/?#]|$)", priority_url)
            if match is None:
                logger.warning("Skipping malformed priority URL: %s", priority_url)
                continue
            priority_id = int(match.group(1))
            if is_permanently_excluded(priority_id) or state.contains(priority_id):
                continue
            if apartments is not None and priority_id in await apartments.published_lalafo_ids(
                [priority_id]
            ):
                continue
            try:
                priority_ad = await client.detail(priority_url)
            except (LalafoError, LalafoParseError, ValueError) as exc:
                logger.warning(
                    "Skipping unavailable priority ad id=%s error=%s",
                    priority_id,
                    type(exc).__name__,
                )
                continue
            allowed, reason = is_allowed(
                priority_ad,
                city=settings.city,
                max_price=SOURCE_MAX_PRICE,
                rooms=SOURCE_ALLOWED_ROOMS,
            )
            if not allowed:
                logger.info("Skipping priority ad id=%s reason=%s", priority_id, reason)
                continue
            if priority_ad.price < max(
                settings.min_price,
                minimum_price_for_rooms(priority_ad.rooms),
            ):
                logger.info("Skipping priority ad id=%s reason=min_price", priority_id)
                continue
            if not priority_ad.no_subletting:
                logger.info("Skipping priority ad id=%s reason=shared_housing", priority_id)
                continue
            if is_substandard_structure(priority_ad):
                logger.info("Skipping priority ad id=%s reason=substandard_structure", priority_id)
                continue
            priority_ad = priority_ad.model_copy(update={"district": district_label})
            duplicate_ids = (
                await apartments.duplicate_candidate_ids([priority_ad])
                if apartments is not None
                else set()
            )
            if priority_id in duplicate_ids:
                continue
            candidates.append(priority_ad)
            candidate_ids.add(priority_id)
            curated_ids.add(priority_id)
            direct_priority_count += 1

        # Publish newly supplied operator cards immediately. Once their IDs are
        # durable, later cycles skip them and resume the full automatic search.
        if direct_priority_count:
            candidate_pool_limit = len(candidates)
            search_urls = (DEFAULT_SEARCH_URL,)
        else:
            search_urls = (DEFAULT_SEARCH_URL, *ADDITIONAL_SEARCH_URLS)
        search_index = 0
        search_url = search_urls[search_index]
        source_targets = source_candidate_targets(
            candidate_pool_limit,
            len(search_urls),
            limit,
        )
        source_candidate_limit = source_targets[search_index]
        page_number = 1
        # Always inspect at least one page from each operator-approved source.
        # Once the shared pool is full, move to the next source for one page
        # instead of spending the whole cycle on the first, larger query.
        while len(candidates) < candidate_pool_limit or search_index < len(search_urls) - 1:
            try:
                page = await client.search(search_url, page=page_number)
            except (LalafoError, LalafoParseError) as exc:
                search_index += 1
                if search_index < len(search_urls):
                    logger.warning(
                        "Lalafo source %d failed on page %d; trying fallback "
                        "source %d: %s",
                        search_index,
                        page_number,
                        search_index + 1,
                        exc,
                    )
                    search_url = search_urls[search_index]
                    source_candidate_limit = source_targets[search_index]
                    page_number = 1
                    continue
                if candidates:
                    logger.warning(
                        "Final realtor source failed after %d durable candidates; "
                        "publishing the partial batch",
                        len(candidates),
                    )
                    break
                logger.error(
                    "All owner and realtor sources are temporarily unavailable; "
                    "deferring publication without an operator alert: %s",
                    exc,
                )
                if engine is not None:
                    await engine.dispose()
                # A blocked/temporarily unavailable catalogue is not a broken
                # publisher. Leave Telegram untouched and try fresh inventory
                # on the next scheduled cycle instead of alarming the operator.
                return 0
            if page_number == 1:
                logger.info("Lalafo source search found %d advertisements", page.total)
            if not page.items:
                search_index += 1
                if search_index >= len(search_urls):
                    break
                search_url = search_urls[search_index]
                source_candidate_limit = source_targets[search_index]
                page_number = 1
                logger.info(
                    "Current source has no more matches; trying fallback source %d",
                    search_index + 1,
                )
                continue
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
            repostable_publications = {}
            repostable_ids = set(repostable_publications)
            repost_last_published_at.update(repostable_publications)
            detail_search_ads = []
            for search_ad in page.items:
                if is_permanently_excluded(search_ad.lalafo_id):
                    logger.info(
                        "Skipping permanently excluded source ad id=%s",
                        search_ad.lalafo_id,
                    )
                    continue
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
                if len(candidates) >= source_candidate_limit:
                    break
                if ad is None:
                    continue
                if is_permanently_excluded(ad.lalafo_id):
                    logger.info("Skipping permanently excluded ad id=%s", ad.lalafo_id)
                    continue
                if ad.lalafo_id in curated_ids:
                    continue
                if ad.lalafo_id in candidate_ids:
                    logger.info("Skipping same-cycle duplicate id=%s", ad.lalafo_id)
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
                if ad.price < max(settings.min_price, minimum_price_for_rooms(ad.rooms)):
                    logger.info("Skipping ad id=%s reason=min_price", ad.lalafo_id)
                    continue
                if len(ad.photo_urls) < SOURCE_MIN_PHOTOS:
                    logger.info(
                        "Skipping ad id=%s reason=too_few_photos count=%d",
                        ad.lalafo_id,
                        len(ad.photo_urls),
                    )
                    continue
                if not ad.no_subletting:
                    logger.info(
                        "Skipping ad id=%s reason=shared_housing",
                        ad.lalafo_id,
                    )
                    continue
                if is_substandard_structure(ad):
                    logger.info(
                        "Skipping ad id=%s reason=substandard_structure",
                        ad.lalafo_id,
                    )
                    continue
                if not settings.allow_no_deposit and ad.deposit is None:
                    logger.info("Skipping ad id=%s reason=deposit", ad.lalafo_id)
                    continue
                if state.contains(ad.lalafo_id, ad_fingerprint(ad)) and not is_repost:
                    continue
                if ad.lalafo_id in duplicate_ids and not is_repost:
                    logger.info("Skipping DB duplicate id=%s", ad.lalafo_id)
                    continue
                candidates.append(ad)
                candidate_ids.add(ad.lalafo_id)
                if is_repost:
                    repost_candidate_ids.add(ad.lalafo_id)
            if len(candidates) >= source_candidate_limit:
                search_index += 1
                if search_index >= len(search_urls):
                    break
                search_url = search_urls[search_index]
                source_candidate_limit = source_targets[search_index]
                page_number = 1
                logger.info("Switching to supplementary Lalafo source %d", search_index + 1)
                continue
            if page_number >= min(
                page.page_count,
                SOURCE_MAX_SEARCH_PAGES,
            ):
                search_index += 1
                if search_index >= len(search_urls):
                    break
                search_url = search_urls[search_index]
                source_candidate_limit = source_targets[search_index]
                page_number = 1
                logger.info("Switching to supplementary Lalafo source %d", search_index + 1)
                continue
            page_number += 1

    managed_profile_candidates = [
        ad for ad in candidates if ad.lalafo_id in managed_profile_ids
    ]
    curated_candidates = [
        ad
        for ad in candidates
        if ad.lalafo_id in curated_ids and ad.lalafo_id not in managed_profile_ids
    ]
    regular_candidates = [
        ad for ad in candidates if ad.lalafo_id not in curated_ids
    ]
    managed_profile_cards = choose_managed_profile_cards(
        managed_profile_candidates,
        limit=min(MANAGED_PROFILE_MAX_PER_RUN, limit),
    )
    profile_cards = deduplicate_candidates(curated_candidates)[
        : max(0, limit - len(managed_profile_cards))
    ]
    two_bedroom_limit = min(
        TWO_BEDROOM_MAX_PER_RUN,
        max(0, TWO_BEDROOM_DAILY_LIMIT - two_bedrooms_published_today),
        max(0, limit - len(profile_cards) - len(managed_profile_cards)),
    )
    two_bedroom_cards = select_publish_batch_with_reposts(
        [ad for ad in regular_candidates if ad.rooms == "2"],
        {},
        two_bedroom_limit,
    )
    owner_middle = select_publish_batch_with_reposts(
        [ad for ad in regular_candidates if ad.rooms in {"1", "studio"}],
        repost_last_published_at,
        max(
            0,
            limit
            - len(profile_cards)
            - len(managed_profile_cards)
            - len(two_bedroom_cards),
        ),
    )
    candidates = insert_randomly(
        mix_room_types(owner_middle + two_bedroom_cards + profile_cards),
        managed_profile_cards,
    )
    repost_candidate_ids.intersection_update(ad.lalafo_id for ad in candidates)
    central_count = sum(is_central_district(ad.district) for ad in candidates)
    central_percent = round(central_count * 100 / len(candidates)) if candidates else 0
    owner_other_count = sum(
        not is_central_district(ad.district) for ad in candidates
    )
    owner_other_percent = (
        round(owner_other_count * 100 / len(candidates)) if candidates else 0
    )
    logger.info(
        "Central-district share: %d/%d (%d%%), target=%d%%",
        central_count,
        len(candidates),
        central_percent,
        round(CENTRAL_BATCH_SHARE * 100),
    )
    logger.info(
        "Other-district share: %d/%d (%d%%), target=%d%%",
        owner_other_count,
        len(candidates),
        owner_other_percent,
        round(OWNER_OTHER_BATCH_SHARE * 100),
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
        support_url=settings.support_bot_url,
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
        results = []
        for index, ad in enumerate(candidates):
            result = await publish_one(ad)
            results.append(result)
            if result[1] is not None and index < len(candidates) - 1:
                logger.info(
                    "Waiting %d seconds before the next Telegram card",
                    SOURCE_PUBLISH_SPACING_SECONDS,
                )
                await asyncio.sleep(SOURCE_PUBLISH_SPACING_SECONDS)
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

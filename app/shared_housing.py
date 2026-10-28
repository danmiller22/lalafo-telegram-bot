"""Separate Telegram-only shared-housing supply and spaced daily quota."""
from datetime import timedelta
import re

from sqlalchemy import select

from app.models import Apartment, ApartmentInventoryQueue
from app.promotions import publication_window
from app.telegram.source_filter import is_lodging_offer

SHARED_ROOMS = "shared"
SHARED_DAILY_TARGET = 10
SHARED_MIN_PRICE = 3_000
SHARED_MAX_PRICE = 25_000
SHARED_GAP = timedelta(minutes=120)


def shared_offer(text: str) -> bool:
    text = (text or "").casefold().replace("ё", "е")
    text = re.sub(r"\bбез\s+подселени[яй]\b", "", text)
    return bool(re.search(r"подсел|койко[ -]?мест", text))


def valid_shared(ad, *, city="Бишкек") -> tuple[bool, str]:
    if not str(ad.source_url).startswith("https://t.me/"):
        return False, "shared_source"
    if ad.city.casefold() != city.casefold() or getattr(ad, "currency", "KGS").upper() != "KGS":
        return False, "shared_city_currency"
    if getattr(ad, "category_id", 2044) != 2044 or ad.no_subletting:
        return False, "shared_category"
    if not SHARED_MIN_PRICE <= ad.price <= SHARED_MAX_PRICE:
        return False, "shared_price"
    if not ad.phone or not ad.photo_urls:
        return False, "shared_contact_or_photos"
    if not shared_offer(ad.source_description) or is_lodging_offer(ad.source_description):
        return False, "shared_offer"
    return True, "ok"


async def shared_deliveries(session, start, end):
    events = publication_window(start, end, include_external_repeats=False)
    return list((await session.execute(select(events.c.published_at).join(
        Apartment, Apartment.id == events.c.apartment_id,
    ).where(Apartment.rooms == SHARED_ROOMS).order_by(events.c.published_at))).scalars())


async def schedule_shared(session, *, now, day_start, day_end) -> int:
    """Keep ten separate slots; refills cannot add a second daily allowance."""
    sent = await shared_deliveries(session, day_start, day_end)
    pending = list((await session.scalars(select(ApartmentInventoryQueue).join(Apartment).where(
        Apartment.rooms == SHARED_ROOMS,
        ApartmentInventoryQueue.status.in_(("queued", "publishing", "uncertain")),
    ))).all())
    remaining = max(0, SHARED_DAILY_TARGET - len(sent) - len(pending))
    if not remaining:
        return 0
    excluded = select(ApartmentInventoryQueue.apartment_id).where(
        ApartmentInventoryQueue.status.in_(("queued", "publishing", "uncertain")))
    stock = list((await session.scalars(select(Apartment).where(
        Apartment.rooms == SHARED_ROOMS, Apartment.active.is_(True),
        Apartment.publication_status == "discovered",
        Apartment.availability_status != "unavailable",
        Apartment.last_seen_at >= now - timedelta(hours=48),
        Apartment.source_url.like("https://t.me/%"),
        Apartment.price.between(SHARED_MIN_PRICE, SHARED_MAX_PRICE),
        Apartment.id.not_in(excluded),
    ).order_by(Apartment.last_seen_at.desc()).limit(100))).all())
    scheduled = 0
    for ad in stock:
        if not valid_shared(ad)[0]:
            continue
        index = len(sent) + len(pending) + scheduled
        # Ten slots across the existing 21-hour publication day.
        slot = day_start + timedelta(minutes=45 + 126 * index)
        slot = max(now, slot)
        if slot >= day_start + timedelta(hours=21) or scheduled >= remaining:
            break
        row = await session.scalar(select(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.apartment_id == ad.id))
        if row is None:
            row = ApartmentInventoryQueue(apartment_id=ad.id)
            session.add(row)
        row.status = "queued"
        row.window_key = "shared:" + day_start.date().isoformat()
        row.sequence = index + 1
        row.scheduled_at = slot
        row.claimed_at = None
        row.last_error = None
        row.attempts = 0
        scheduled += 1
    return scheduled

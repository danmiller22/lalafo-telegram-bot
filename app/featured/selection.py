from __future__ import annotations

from datetime import datetime

from app.lalafo.models import LalafoAd

PRIORITY_TERMS = (
    "цум", "гум", "дордой плаза", "филармони", "бишкек парк",
    "ала-тоо", "золотой квадрат", "ошский", "ош базар",
    "молодая гвардия", "восток-5", "восток 5", "аламедин-1", "аламедин 1",
)
CENTRAL_TERMS = PRIORITY_TERMS + (
    "центр", "эркиндик", "карпинка", "пишпек", "политех", "бгу",
)


def _normalized(value: str | None) -> str:
    return (value or "").casefold().replace("ё", "е")


def district_tier(district: str | None) -> int:
    value = _normalized(district)
    if any(term in value for term in PRIORITY_TERMS):
        return 2
    return 1 if any(term in value for term in CENTRAL_TERMS) else 0


def quality_key(ad: LalafoAd, priority_price: int = 30_000) -> tuple[object, ...]:
    freshness = ad.source_updated_at.timestamp() if ad.source_updated_at else 0.0
    complete = bool(ad.district and ad.rooms and ad.phone)
    return (
        district_tier(ad.district),
        ad.price <= priority_price,
        -ad.price,
        len(ad.photo_urls) >= 4,
        len(ad.photo_urls),
        freshness,
        complete,
    )


def select_featured(
    candidates: list[LalafoAd],
    *,
    count: int = 2,
    priority_price: int = 30_000,
    max_price: int = 35_000,
    recent_source_ids: set[int] | None = None,
) -> list[LalafoAd]:
    recent = recent_source_ids or set()
    eligible = [
        ad for ad in candidates
        if ad.lalafo_id not in recent
        and ad.price <= max_price
        and bool(ad.district and ad.phone and ad.photo_urls)
    ]
    eligible.sort(key=lambda ad: quality_key(ad, priority_price), reverse=True)
    selected: list[LalafoAd] = []
    phones: set[str] = set()
    for ad in eligible:
        if ad.phone in phones:
            continue
        selected.append(ad)
        phones.add(ad.phone)
        if len(selected) >= count:
            break
    return selected


def build_description(ad: LalafoAd) -> str:
    subletting = "Без подселения" if ad.no_subletting else "С подселением"
    rooms = {"studio": "Студия", "1": "1-комнатная квартира", "2": "2-комнатная квартира"}.get(
        ad.rooms.casefold(), ad.rooms
    )
    return "\n".join((
        f"🏠 {rooms}", f"📍 {ad.district}", "🏙 Бишкек",
        f"💰 {ad.price:,} сом".replace(",", " "), f"🚪 {subletting}",
    ))

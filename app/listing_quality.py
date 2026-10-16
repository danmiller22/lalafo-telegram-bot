"""Apartment selection and publication ordering preferences."""
from __future__ import annotations

import random
import re
from collections import Counter


def rental_value_band(price: int, rooms: str, description: str | None = None) -> int:
    """A selection preference, not an estimate of market value or availability.

    Prefer studios up to 28k and one-room flats up to 30k; use 30–35k
    next, then the remaining allowed stock. Explicitly unfurnished flats
    need a lower rent to compete with furnished/unspecified alternatives.
    """
    cheap_limit = 28_000 if rooms == 'studio' else 30_000
    band = 0 if price <= cheap_limit else 1 if price <= 35_000 else 2
    text = (description or '').casefold().replace('ё', 'е')
    unfurnished = bool(re.search(r'\bбез\s+мебели\b|\bпустая\s+квартира\b', text))
    return band + int(unfurnished)


def district_priority(district: str | None) -> int:
    text = re.sub(r"[-–—]", " ", (district or "").casefold().replace("ё", "е"))
    if "филармони" in text or re.search(r"восток\s*5(?!\d)", text):
        return 3
    central = (
        "центр", "золотой квадрат", "моссовет", "филармони", "эркиндик",
        "цум", "гум", "восток 5", "бишкек парк", "дордой плаза",
        "dordoi plaza", "вефа", "азия молл", "караван", "ала тоо",
    )
    if any(term in text for term in central):
        return 2
    if any(term in text for term in ("асанбай", "джал", "тунгуч", "юг 2", "южн")) or re.search(
        r"(?<!\d)(?:[3-9]|1[0-2])\s*(?:мкр|м[и]?крорайон)", text
    ):
        return 1
    return 0


def price_bucket(price: int) -> int:
    return min(2, max(0, (price - 25_000) // 5_000))


def mix_prices(items: list, *, rng=None) -> list:
    """Interleave available price ranges, shuffling cards within each range."""
    rng = rng or random.SystemRandom()
    buckets: dict[int, list] = {}
    for item in items:
        buckets.setdefault(price_bucket(item.price), []).append(item)
    for cards in buckets.values():
        rng.shuffle(cards)
    result = []
    counts: Counter[int] = Counter()
    previous = None
    while buckets:
        available = list(buckets)
        rng.shuffle(available)
        selected = min(available, key=lambda bucket: (bucket == previous, counts[bucket]))
        result.append(buckets[selected].pop())
        counts[selected] += 1
        previous = selected
        if not buckets[selected]:
            del buckets[selected]
    return result

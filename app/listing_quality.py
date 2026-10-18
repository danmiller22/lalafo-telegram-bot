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
    # Confirmed-payment leaders from both comparison weeks. District mixing
    # still limits repetition while these cards win equivalent price bands.
    if re.search(r"восток\s*5(?!\d)", text):
        return 6
    if any(term in text for term in (
        "центр", "филармони", "бишкек парк", "азия молл",
    )):
        return 5
    if any(term in text for term in (
        "цум", "гум", "политех", "дордой плаза", "аламедин 1",
    )):
        return 4
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
    if price < 30_000:
        return 0
    if price <= 35_000:
        return 1
    return 2


PRICE_MIX_WEIGHTS = {0: 2, 1: 3, 2: 1}


def price_mix_rank(bucket: int, counts: dict[int, int]) -> float:
    """Prefer 30–35k, retain cheaper cards, and never block fallback stock."""
    return (counts.get(bucket, 0) + 1) / PRICE_MIX_WEIGHTS[bucket]


def mix_prices(items: list, *, rng=None) -> list:
    """Interleave every range, led by owner-friendly 30–35k stock."""
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
        selected = min(available, key=lambda bucket: (price_mix_rank(bucket, counts), bucket == previous))
        result.append(buckets[selected].pop())
        counts[selected] += 1
        previous = selected
        if not buckets[selected]:
            del buckets[selected]
    return result

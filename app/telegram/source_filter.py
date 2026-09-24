from __future__ import annotations

import re

PRICE_RE = re.compile(r"(?<!\d)(\d{2,3}(?:[ .]\d{3})|\d{4,6})(?:\s*(?:сом|сомов|с|kgs|кгс))?", re.I)
THOUSANDS_PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*тыс(?:яч[аи]?)?\.?\b",
    re.I,
)
SEARCH_TERMS = ("ищу квартиру", "сниму квартиру", "нужна квартира", "ищем квартиру", "ищу жилье")
OFFER_TERMS = ("сдам", "сдается", "сдаётся", "сдаю", "квартира в аренду", "квартиру в аренду", "продам квартиру")
OWNER_TERMS = (
    "собственник", "хозяин", "хозяйка", "без посредников", "от хозяина",
    "агентствам не беспокоить", "риелторам не беспокоить", "без риелторов",
)


def extract_kgs_price(text: str) -> int | None:
    values: list[int] = []
    for match in PRICE_RE.finditer(text or ""):
        value = int(re.sub(r"\D", "", match.group(1)))
        if 5_000 <= value <= 500_000:
            values.append(value)
    for match in THOUSANDS_PRICE_RE.finditer(text or ""):
        value = round(float(match.group(1).replace(",", ".")) * 1_000)
        if 5_000 <= value <= 500_000:
            values.append(value)
    return min(values) if values else None


def is_apartment_offer(text: str, *, max_price: int = 40_000) -> bool:
    normalized = " ".join((text or "").casefold().replace("ё", "е").split())
    if not normalized or any(term in normalized for term in SEARCH_TERMS):
        return False
    if not any(term in normalized for term in OFFER_TERMS):
        return False
    price = extract_kgs_price(normalized)
    return price is not None and price <= max_price


def is_owner_offer(text: str) -> bool:
    normalized = (text or "").casefold().replace("ё", "е")
    return any(term in normalized for term in OWNER_TERMS)

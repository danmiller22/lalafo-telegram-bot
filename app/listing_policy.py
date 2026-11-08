"""Product rules for choosing apartments for the public Telegram feed."""
from __future__ import annotations

import re
from typing import Any


PUBLIC_ROOMS = ("studio", "1", "2")

RENT_MIN_PRICE = 25_000
RENT_MAX_PRICE = 40_000
OWNER_MIN_PRICE = REALTOR_MIN_PRICE = RENT_MIN_PRICE
OWNER_MAX_PRICE = REALTOR_MAX_PRICE = RENT_MAX_PRICE

CENTRAL_REALTOR_TERMS = (
    "центр",
    "золотой квадрат",
    "площадь ала тоо",
    "эркиндик",
    "филармони",
    "цум",
    "гум",
    "восток 5",
    "бишкек парк",
    "дордой плаза",
    "вефа",
    "моссовет",
    "манаса боконбаева",
    "киевская уметалиева",
    "азия молл",
    "политех",
    "аламедин 1",
    "юг 2",
    "7 мкр",
)


def normalized_area(value: str | None) -> str:
    return re.sub(
        r"[^\w]+", " ", (value or "").casefold().replace("ё", "е")
    ).strip()


def is_central_realtor_area(district: str | None) -> bool:
    area = normalized_area(district)
    return any(term in area for term in CENTRAL_REALTOR_TERMS)


def author_policy_eligible(*, seller_type: str | None, owner_listing: bool) -> bool:
    """Accept unknown authors and confirmed owners, never confirmed realtors."""
    return seller_type in {None, "unknown"} or (seller_type == "owner" and owner_listing)


def seller_policy_eligible(
    *, seller_type: str | None, owner_listing: bool, price: int, district: str | None
) -> bool:
    return author_policy_eligible(seller_type=seller_type, owner_listing=owner_listing) and OWNER_MIN_PRICE <= price <= OWNER_MAX_PRICE


def listing_policy_eligible(ad: Any) -> bool:
    return seller_policy_eligible(
        seller_type=getattr(ad, "seller_type", "unknown"),
        owner_listing=bool(getattr(ad, "owner_listing", False)),
        price=int(getattr(ad, "price", 0) or 0),
        district=getattr(ad, "district", None),
    )

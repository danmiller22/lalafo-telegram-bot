"""Product rules for choosing apartments for the public Telegram feed."""
from __future__ import annotations

import re
from typing import Any


OWNER_MIN_PRICE = 25_000
OWNER_MAX_PRICE = 45_000
REALTOR_MIN_PRICE = 25_000
REALTOR_MAX_PRICE = 35_000

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
)


def normalized_area(value: str | None) -> str:
    return re.sub(
        r"[^\w]+", " ", (value or "").casefold().replace("ё", "е")
    ).strip()


def is_central_realtor_area(district: str | None) -> bool:
    area = normalized_area(district)
    return any(term in area for term in CENTRAL_REALTOR_TERMS)


def seller_policy_eligible(
    *, seller_type: str | None, owner_listing: bool, price: int, district: str | None
) -> bool:
    """Accept broad owner stock and only inexpensive central realtor stock."""
    if seller_type == "owner" and owner_listing:
        return OWNER_MIN_PRICE <= price <= OWNER_MAX_PRICE
    if seller_type == "realtor":
        return (
            REALTOR_MIN_PRICE <= price <= REALTOR_MAX_PRICE
            and is_central_realtor_area(district)
        )
    return False


def listing_policy_eligible(ad: Any) -> bool:
    return seller_policy_eligible(
        seller_type=getattr(ad, "seller_type", "unknown"),
        owner_listing=bool(getattr(ad, "owner_listing", False)),
        price=int(getattr(ad, "price", 0) or 0),
        district=getattr(ad, "district", None),
    )

from __future__ import annotations

from typing import Any

from app.featured.selection import build_description
from app.lalafo.models import LalafoAd

DISTRICT_PARAM_ID = 357
ROOM_PARAM_ID = 69
ROOM_VALUES = {"studio": 2772, "1": 2773, "2": 2774}
DISTRICT_VALUES = {
    "цум": 23248, "гум": 23248, "дордой плаза": 56389,
    "филармони": 23214, "бишкек парк": 30256, "ала-тоо": 51199,
    "золотой квадрат": 45284, "ошский": 23212, "ош базар": 23212,
    "молодая гвардия": 45275, "восток-5": 23200, "восток 5": 23200,
    "аламедин-1": 23245, "аламедин 1": 23245,
}


def posting_payload(ad: LalafoAd) -> dict[str, Any]:
    """Build a minimal non-contradictory payload matching the account template."""
    params: list[dict[str, Any]] = [
        {"id": 5406, "value": 76935},  # economy
        {"id": 2149, "value": 42340},  # agency
        {"id": 2218, "value": 43128},  # owner-contact service
        {"id": 867, "value": 29515},   # standard series
        {"id": 872, "value": 51355},   # fresh repair
        {"id": 68, "value": 30756},    # fully furnished
    ]
    room_value = ROOM_VALUES.get(ad.rooms.casefold())
    if room_value:
        params.append({"id": ROOM_PARAM_ID, "value": room_value})
    normalized_district = (ad.district or "").casefold().replace("ё", "е")
    district_value = next(
        (value for term, value in DISTRICT_VALUES.items() if term in normalized_district),
        None,
    )
    if district_value:
        params.append({"id": DISTRICT_PARAM_ID, "value": district_value})
    return {
        "category_id": 2044,
        "city_id": 103184,
        "currency": "KGS",
        "price": ad.price,
        "description": build_description(ad),
        "params": params,
    }


def has_conflicting_params(payload: dict[str, Any]) -> bool:
    values = {item.get("value") for item in payload.get("params", []) if isinstance(item, dict)}
    conflicts = ({"balcony", "no_balcony"}, {"corner", "not_corner"},
                 {"separate_bathroom", "combined_bathroom"}, {"gas", "electric"})
    return any(pair <= values for pair in conflicts)

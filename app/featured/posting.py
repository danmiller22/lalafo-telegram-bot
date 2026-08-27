from __future__ import annotations

from typing import Any

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
    "3 мкр": 30232, "4 мкр": 30233, "5 мкр": 30234,
    "6 мкр": 30235, "7 мкр": 30236, "8 мкр": 30237,
    "10 мкр": 30227, "11 мкр": 30229, "12 мкр": 30231,
}

TERM_VALUES = [30783, 30782, 30781, 30780]
APPLIANCE_VALUES = [
    23055, 30763, 30765, 30758, 78193, 30760, 30759, 30766,
    30764, 30761, 30632, 78194, 30571, 30762, 30767,
]
AMENITY_VALUES = [
    78207, 30575, 30771, 30772, 30629, 30630, 30768, 29594,
    30773, 30770, 28811, 30769, 28675, 27634, 30774,
]
EXTRA_VALUES = [
    53235, 53238, 29584, 53236, 29573, 81541, 29588, 53237,
    29578, 29585, 81543, 81542,
]
COMMUNICATION_VALUES = [29532, 29535, 52860, 29533]


def short_description(ad: LalafoAd) -> str:
    rooms = {
        "studio": "Студия", "1": "1-комнатная квартира",
        "2": "2-комнатная квартира",
    }.get(ad.rooms.casefold(), ad.rooms)
    lines = [
        f"🏠 {rooms}",
        *([f"📍 {ad.district}"] if ad.district else []),
        "🏙 Бишкек",
        f"💰 {ad.price:,} сом".replace(",", " "),
        f"🚪 {'Без подселения' if ad.no_subletting else 'С подселением'}",
    ]
    if ad.deposit:
        lines.append(f"🔐 Депозит: {ad.deposit:,} сом".replace(",", " "))
    lines.append("🛋 Полностью меблирована, все условия")
    return "\n".join(lines)


def posting_payload(ad: LalafoAd) -> dict[str, Any]:
    """Build the complete account template while keeping contact chat-only."""
    params: list[dict[str, Any]] = [
        {"id": 5406, "value": 76935},  # economy
        {"id": 2149, "value": 42340},  # agency
        {"id": 2218, "value": 43128},  # owner-contact service
        {"id": 867, "value": 29515},   # standard series
        {"id": 872, "value": 51355},   # fresh repair
        {"id": 68, "value": 30756},    # fully furnished
        {"id": 951, "value": TERM_VALUES},
        {"id": 948, "value": APPLIANCE_VALUES},
        {"id": 949, "value": AMENITY_VALUES},
        {"id": 878, "value": EXTRA_VALUES},
        {"id": 870, "value": COMMUNICATION_VALUES},
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
    if ad.deposit:
        params.append({"id": 947, "value": ad.deposit})
    return {
        "category_id": 2044,
        "city_id": 103184,
        "currency": "KGS",
        "price": ad.price,
        "description": short_description(ad),
        "hide_phone": True,
        "hide_chat": False,
        "params": params,
    }


def posting_preview(ad: LalafoAd) -> str:
    payload = posting_payload(ad)
    return "\n".join((
        "🧾 <b>Вот что будет опубликовано на Lalafo</b>",
        "",
        str(payload["description"]),
        "",
        "🏷 Класс: Эконом",
        "🏢 Кто предлагает: Агентство недвижимости",
        "🔑 Услуга: Выдача контактов/адреса собственника",
        "🛋 Мебель: полностью",
        "📺 Техника: все доступные пункты",
        "✨ Удобства и условия: все доступные пункты",
        "📅 Срок: от 1 месяца / 3 месяцев / 6 месяцев / 1 года",
        "💬 Связь: <b>только чат Lalafo</b>; телефон скрыт",
        f"📷 Фотографий: {len(ad.photo_urls)}",
        "",
        "Объявление пока не опубликовано и реклама не оплачена.",
    ))


def has_conflicting_params(payload: dict[str, Any]) -> bool:
    ids = [item.get("id") for item in payload.get("params", []) if isinstance(item, dict)]
    return len(ids) != len(set(ids))

from __future__ import annotations

import re
from decimal import Decimal

from app.models import ApartmentSearchProfile


def parse_budget(text: str) -> int | None:
    """Accept one amount; never concatenate room counts or budget ranges."""
    if len(text) > 64:
        return None
    match = re.fullmatch(
        r"\s*(?:до\s+)?(\d+(?:[ \u00a0\u202f]\d{3})*|\d+[.,]\d+)\s*"
        r"(тыс\.?|тысяч(?:и)?|к|k)?\s*(?:сом(?:ов)?|kgs|с)?\s*",
        text.casefold(),
    )
    if match is None:
        return None
    number = re.sub(r"[ \u00a0\u202f]", "", match[1]).replace(",", ".")
    amount = Decimal(number) * (1000 if match[2] else 1)
    return int(amount) if amount == amount.to_integral_value() else None


def profile_summary(profile: ApartmentSearchProfile) -> str:
    room_labels = {"studio": "студия", "1": "1-комнатная", "2": "2-комнатная"}
    rooms = ", ".join(room_labels[value] for value in profile.rooms if value in room_labels)
    districts = "весь Бишкек" if profile.all_districts else ", ".join(profile.districts)
    if len(districts) > 1200:
        districts = districts[:1197] + "…"
    sellers = set(profile.seller_types or ["owner", "realtor"])
    authors = (
        "собственники и агентства"
        if len(sellers) == 2
        else ("собственники" if "owner" in sellers else "агентства")
    )
    budget = f"{profile.max_budget:,}".replace(",", " ")
    alerts = "включены" if profile.notifications_enabled else "приостановлены"
    return (
        f"🔎 Ваш поиск\n\n"
        f"🏠 {rooms}\n📍 {districts}\n💰 До {budget} сом/месяц\n"
        f"Объявления: {authors}\n🔔 Уведомления {alerts}"
    )

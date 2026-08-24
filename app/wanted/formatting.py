from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.models import WantedAd
from app.telegram.formatting import format_money


ROOM_LABELS = {
    "studio": "Студия",
    "1": "1 комната",
    "2": "2 комнаты",
    "3": "3 комнаты",
    "4+": "4+ комнаты",
}


def room_label(value: str) -> str:
    return ROOM_LABELS.get(value, value)


def format_wanted_preview(data: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "🔎 Ищу",
            "",
            f"🏠 Комнаты: {room_label(str(data['rooms']))}",
            f"📍 Район: {data['district']}",
            f"💰 Бюджет: до {format_money(int(data['budget']))} сом/мес.",
            f"📅 Заселение: {data['move_in']}",
            f"👥 Кто будет жить: {data['tenants']}",
            f"📝 Дополнительно: {data['notes']}",
            f"📞 Контакты: {data['contact']}",
        ]
    )


def format_wanted_ad(ad: WantedAd) -> str:
    return format_wanted_preview(
        {
            "rooms": ad.rooms,
            "district": ad.district,
            "budget": ad.budget,
            "move_in": ad.move_in,
            "tenants": ad.tenants,
            "notes": ad.notes,
            "contact": ad.contact,
        }
    )


def format_wanted_admin(ad: WantedAd) -> str:
    user = f"@{ad.username}" if ad.username else f"{ad.first_name or 'Клиент'} ({ad.telegram_user_id})"
    return "\n".join(
        [
            "💳 Проверка оплаты заявки — 100 сом",
            f"👤 {user}",
            f"🆔 Заявка #{ad.id}",
            "",
            format_wanted_ad(ad),
        ]
    )


def format_wanted_public(ad: WantedAd) -> str:
    return format_wanted_ad(ad)

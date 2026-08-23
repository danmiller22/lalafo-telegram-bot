from __future__ import annotations

from app.lalafo.models import LalafoAd
from app.models import Apartment, PaymentRequest


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def room_title(rooms: str) -> str:
    return {
        "studio": "Студия",
        "1": "1-комнатная квартира",
        "2": "2-комнатная квартира",
    }.get(rooms, "Квартира")


def format_apartment(ad: LalafoAd | Apartment) -> str:
    lines = [f"🏠 {room_title(ad.rooms)}"]
    if ad.district:
        lines.append(f"📍 {ad.district}")
    lines.append(f"🏙 {ad.city}")
    lines.append(f"💰 {format_money(ad.price)} сом")
    if ad.deposit is not None:
        lines.append(f"🔐 Депозит: {format_money(ad.deposit)} сом")
    return "\n".join(lines)


def user_label(request: PaymentRequest) -> str:
    if request.username:
        return f"@{request.username}"
    return f"{request.first_name or 'Пользователь'} ({request.telegram_user_id})"


def format_admin_card(request: PaymentRequest) -> str:
    apartment = request.apartment
    lines = [
        "💳 Проверка оплаты",
        "",
        f"🏠 Квартира #{apartment.id}",
        f"👤 {user_label(request)}",
        "💰 Контакт: 100 сом",
    ]
    if apartment.district:
        lines.append(f"📍 {apartment.district}")
    lines.append(f"🏙 {apartment.city}")
    return "\n".join(lines)


def format_admin_decision(request: PaymentRequest, approved: bool) -> str:
    return "\n".join(
        [
            "✅ Оплата подтверждена" if approved else "❌ Оплата отклонена",
            "",
            f"🏠 Квартира #{request.apartment.id}",
            f"👤 {user_label(request)}",
        ]
    )

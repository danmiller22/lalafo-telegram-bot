from __future__ import annotations

from app.lalafo.models import LalafoAd
from app.models import Apartment, PaymentRequest
from app.payment_plans import plan_label, plan_price


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
    else:
        lines.extend(("📍 Золотой Квадрат", "Дем"))
    lines.append(f"🏙 {ad.city}")
    lines.append(f"💰 {format_money(ad.price)} сом")
    if ad.deposit is not None:
        lines.append(f"🔐 Депозит: {format_money(ad.deposit)} сом")
    return "\n".join(lines)


def format_public_apartment(ad: LalafoAd | Apartment, *, bot_username: str) -> str:
    username = bot_username.lstrip("@")
    return f"{format_apartment(ad)}\n\n🔎 Ищете квартиру? Подайте заявку: @{username}"


def user_label(request: PaymentRequest) -> str:
    if request.username:
        return f"@{request.username} (ID {request.telegram_user_id})"
    return f"{request.first_name or 'Пользователь'} ({request.telegram_user_id})"


def format_admin_card(request: PaymentRequest) -> str:
    apartment = request.apartment
    payment_line = (
        f"💰 Оплата: {plan_price(request.plan)} сом"
        if request.plan == "week"
        else "💰 Архивный тариф отключён"
    )
    lines = [
        "💳 Проверка оплаты",
        "",
        f"🏠 Квартира #{apartment.id}",
        f"👤 {user_label(request)}",
        f"💳 Тариф: {plan_label(request.plan)}",
        payment_line,
        "🧾 Чек прикреплён клиентом",
    ]
    if apartment.district:
        lines.append(f"📍 {apartment.district}")
    lines.append(f"🏙 {apartment.city}")
    return "\n".join(lines)


def format_admin_decision(request: PaymentRequest, approved: bool) -> str:
    plan_line = (
        f"💳 {plan_label(request.plan)} · {plan_price(request.plan)} сом"
        if request.plan == "week"
        else "💳 Архивный тариф отключён"
    )
    return "\n".join(
        [
            "✅ Оплата подтверждена" if approved else "❌ Оплата отклонена",
            "",
            f"🏠 Квартира #{request.apartment.id}",
            f"👤 {user_label(request)}",
            plan_line,
        ]
    )

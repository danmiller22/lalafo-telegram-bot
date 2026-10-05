from __future__ import annotations

from html import escape

from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.models import Apartment


def format_money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def room_title(rooms: str) -> str:
    return {
        "studio": "Студия",
        "1": "1-комнатная квартира",
        "2": "2-комнатная квартира",
    }.get(rooms, "Квартира")


def unknown_author_label(*, phone: str, price: int, district: str | None, rooms: str) -> str:
    """Use the requested public fallback when the source does not name the author."""
    del phone, price, district, rooms
    return "возможно собственник"


def is_confirmed_owner(ad: LalafoAd | Apartment) -> bool:
    return (
        getattr(ad, "seller_type", None) == "owner"
        and bool(getattr(ad, "owner_listing", False))
    )


def is_supported_source(ad: LalafoAd | Apartment) -> bool:
    url = str(getattr(ad, "source_url", "") or "").casefold()
    return url.startswith((
        "https://lalafo.kg/",
        "https://www.lalafo.kg/",
        "https://t.me/",
        "manual://telegram/",
    ))


def author_label(ad: LalafoAd | Apartment) -> str | None:
    """Label known owners; the check refers to a phone matched to its source."""
    if not is_confirmed_owner(ad):
        return None
    source_url = str(getattr(ad, "source_url", "") or "").casefold()
    if (
        source_url.startswith(("https://lalafo.kg/", "https://www.lalafo.kg/"))
        and getattr(ad, "phone_source_version", 0) == PHONE_SOURCE_VERSION
        and str(getattr(ad, "phone", "") or "").strip()
    ):
        return "Собственник. Контакты проверены ✅"
    return "Собственник"


def seller_status(ad: LalafoAd | Apartment) -> str:
    """Backward-compatible value for internal callers and older tests."""
    return author_label(ad) or "неизвестно"


def format_apartment(ad: LalafoAd | Apartment) -> str:
    lines = [f"🏠 {room_title(ad.rooms)}"]
    if ad.district:
        lines.append(f"📍 {ad.district}")
    else:
        lines.append("📍 Центр")
    lines.append(f"🏙 {ad.city}")
    lines.append(f"💰 {format_money(ad.price)} сом")
    if ad.deposit is not None:
        lines.append(f"🔐 Депозит: {format_money(ad.deposit)} сом")
    label = author_label(ad)
    if label:
        lines.append(label)
    return "\n".join(lines)


def format_public_apartment(
    ad: LalafoAd | Apartment,
    *,
    bot_username: str,
    wanted_deep_link: bool = False,
) -> str:
    username = bot_username.lstrip("@")
    if wanted_deep_link:
        apartment = escape(format_apartment(ad))
        url = escape(f"https://t.me/{username}?start=want", quote=True)
        mention = f'<a href="{url}">@{escape(username)}</a>'
        return f"{apartment}\n\n🔎 Ищете квартиру? Подайте заявку: {mention}"
    return f"{format_apartment(ad)}\n\n🔎 Ищете квартиру? Подайте заявку: @{username}"

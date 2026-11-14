from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.security import TokenSigner
from app.payment_plans import LIFETIME_PRICE, WEEK_PRICE


APARTMENT_KEYBOARD_VERSION = 20
MINI_APP_SHORT_NAME = "access"


def masked_contact_label(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not 9 <= len(digits) <= 15:
        return "Получить номер"
    return ("+" if (phone or "").strip().startswith("+") else "") + digits[:-3] + "***"


def _support_row(support_url: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)]


def _support_and_privacy_row(support_url: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="🛟 Техподдержка", url=support_url),
        InlineKeyboardButton(text="🔒 Политика", url=support_url.split("?", 1)[0] + "?start=privacy"),
    ]


def apartment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    bot_username: str,
    support_url: str,
    include_duplicate: bool = False,
    phone: str | None = None,
    confirmed_owner: bool = False,
) -> InlineKeyboardMarkup:
    payment_token = signer.sign_start_id("miniapp-apartment", apartment_id)
    bot_url = f"https://t.me/{bot_username.lstrip('@')}"
    mini_app_url = f"{bot_url}/{MINI_APP_SHORT_NAME}?startapp={payment_token}"
    rows = [
        [
            InlineKeyboardButton(
                text=masked_contact_label(phone) if confirmed_owner else "Получить номер",
                url=mini_app_url,
            )
        ]
    ]
    if include_duplicate:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 Получить копию в личку",
                    callback_data=f"dup:{signer.sign_id('duplicate', apartment_id)}",
                )
            ]
        )
    rows.append(_support_row(support_url))
    rows.append([InlineKeyboardButton(
        text="🔎 Подобрать по бюджету и району", url=f"{bot_url}?start=search"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def matching_apartment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    bot_username: str,
    phone: str | None = None,
    confirmed_owner: bool = False,
) -> InlineKeyboardMarkup:
    """Compact card keyboard for the private personalized feed."""
    return showcase_apartment_keyboard(
        apartment_id, signer=signer, bot_username=bot_username,
        phone=phone, confirmed_owner=confirmed_owner,
    )


def terms_keyboard(apartment_id: int, *, signer: TokenSigner, bot_username: str) -> InlineKeyboardMarkup:
    bot_url = f"https://t.me/{bot_username.lstrip('@')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Я ознакомлен(а) с условиями и согласен(на) со всеми пунктами",
            callback_data=f"terms:accept:{signer.sign_id('terms', apartment_id)}",
        )],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=f"{bot_url}?start=privacy")],
        [InlineKeyboardButton(text="🛟 Техподдержка", url=f"{bot_url}?start=support")],
    ])


def payment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    price: int = WEEK_PRICE,
) -> InlineKeyboardMarkup:
    paid_token = signer.sign_id("paid", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {price} сом", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="📞 Получить номер",
                    callback_data=f"paid:{paid_token}",
                )
            ],
            _support_and_privacy_row(support_url),
        ]
    )


def payment_reminder_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    price: int = WEEK_PRICE,
    apartments_url: str = "https://t.me/bishkekarendakg",
    manual_claim: bool = True,
) -> InlineKeyboardMarkup:
    """Recovery controls that remain useful outside the Mini App window."""
    rows = [[InlineKeyboardButton(text=f"💳 Оплатить {price} сом", url=payment_url)]]
    if manual_claim:
        rows.append([
            InlineKeyboardButton(
                text="✅ Я оплатил(а)",
                callback_data=f"paid:{signer.sign_id('paid', apartment_id)}",
            )
        ])
    rows.extend([
        [InlineKeyboardButton(text="🏠 Смотреть актуальные квартиры", url=apartments_url)],
        _support_row(support_url),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def private_payment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    pending: bool = False,
    monthly_payment_url: str = "",
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"Недельный тариф — {WEEK_PRICE} сом",
        callback_data=f"plan:w:{signer.sign_id('plan-week', apartment_id)}",
    )]]
    rows.append(
        [
            InlineKeyboardButton(
                text="🔒 Политика конфиденциальности",
                url=support_url.split("?", 1)[0] + "?start=privacy",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def showcase_apartment_keyboard(
    apartment_id: int, *, signer: TokenSigner, bot_username: str = "arenda312bot",
    phone: str | None = None, confirmed_owner: bool = False, saved: bool = False
) -> InlineKeyboardMarkup:
    payment_token = signer.sign_start_id("miniapp-apartment", apartment_id)
    mini_app_url = (
        f"https://t.me/{bot_username.lstrip('@')}/{MINI_APP_SHORT_NAME}"
        f"?startapp={payment_token}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=masked_contact_label(phone) if confirmed_owner else "📞 Получить номер",
                url=mini_app_url,
            )],
            [InlineKeyboardButton(
                text="✅ Сохранено" if saved else "❤️ Сохранить",
                callback_data=(
                    "favorite:noop" if saved else
                    f"listing:save:{signer.sign_id('listing-save', apartment_id)}"
                ),
            )],
        ]
    )


def favorite_apartment_keyboard(
    apartment_id: int, *, signer: TokenSigner, available: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if available:
        rows.append([InlineKeyboardButton(
            text="📞 Получить номер",
            callback_data=(
                f"listing:contact:{signer.sign_id('listing-contact', apartment_id)}"
            ),
        )])
    rows.append([InlineKeyboardButton(
        text="🗑 Удалить из избранного",
        callback_data=f"favorite:remove:{signer.sign_id('favorite-remove', apartment_id)}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def customer_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔎 Найти выгодные и дешевые квартиры", callback_data="menu:cheap"
        )],
        [InlineKeyboardButton(text="❤️ Избранные", callback_data="menu:favorites")],
        [InlineKeyboardButton(text="🛟 Техподдержка", callback_data="menu:support")],
        [InlineKeyboardButton(text="ℹ️ О сервисе и услугах", callback_data="menu:about")],
        [InlineKeyboardButton(
            text="🔒 Политика конфиденциальности", callback_data="menu:privacy"
        )],
    ])


def private_contact_keyboard(*, support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _support_row(support_url),
        ]
    )


def finik_keyboard(payment_redirect_url: str, *, support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {WEEK_PRICE} сом", url=payment_redirect_url)],
            _support_row(support_url),
        ]
    )


def status_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    price: int = WEEK_PRICE,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {price} сом", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="📞 Получить номер",
                    callback_data=f"paid:{signer.sign_id('paid', apartment_id)}",
                )
            ],
            _support_row(support_url),
        ]
    )


def reveal_keyboard(
    apartment_id: int, *, signer: TokenSigner, support_url: str
) -> InlineKeyboardMarkup:
    token = signer.sign_id("view", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📞 Показать номер",
                    callback_data=f"view:{token}",
                )
            ],
            _support_row(support_url),
        ]
    )

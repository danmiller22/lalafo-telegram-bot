from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.security import TokenSigner
from app.payment_plans import MONTH_PRICE, WEEK_PRICE


APARTMENT_KEYBOARD_VERSION = 13
MINI_APP_SHORT_NAME = "access"


def _support_row(support_url: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)]


def apartment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    bot_username: str,
    support_url: str,
    include_duplicate: bool = False,
) -> InlineKeyboardMarkup:
    payment_token = signer.sign_start_id("miniapp-apartment", apartment_id)
    bot_url = f"https://t.me/{bot_username.lstrip('@')}"
    mini_app_url = f"{bot_url}/{MINI_APP_SHORT_NAME}?startapp={payment_token}"
    rows = [
            [
                InlineKeyboardButton(
                    text="Получить номер",
                    url=mini_app_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Подать заявку на поиск квартиры",
                    url=f"{bot_url}?start=want",
                )
            ],
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
                    text="✅ Я оплатил",
                    callback_data=f"paid:{paid_token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату / Получить номер",
                    callback_data=f"view:{signer.sign_id('view', apartment_id)}",
                )
            ],
            _support_row(support_url),
        ]
    )


def private_payment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    pending: bool = False,
    monthly_payment_url: str = "",
) -> InlineKeyboardMarkup:
    rows = [
            [
                InlineKeyboardButton(
                    text=f"Базовая: 7 дней — {WEEK_PRICE} сом",
                    callback_data=f"plan:w:{signer.sign_id('plan-week', apartment_id)}",
                )
            ],
        ]
    if monthly_payment_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Премиум: 30 дней — {MONTH_PRICE} сом",
                    callback_data=f"plan:m:{signer.sign_id('plan-month', apartment_id)}",
                )
            ]
        )
    rows.append(_support_row(support_url))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receipt_payment_keyboard(
    apartment_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {WEEK_PRICE} сом", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил(а)", callback_data="receipt:send"
                )
            ],
            _support_row(support_url),
        ]
    )


def pending_payment_keyboard(
    apartment_id: int, *, signer: TokenSigner, support_url: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏳ Чек проверяется",
                    callback_data=f"view:{signer.sign_id('view', apartment_id)}",
                )
            ],
            _support_row(support_url),
        ]
    )


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


def paid_keyboard(
    apartment_id: int, *, signer: TokenSigner, support_url: str
) -> InlineKeyboardMarkup:
    token = signer.sign_id("paid", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{token}")],
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
    token = signer.sign_id("view", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {price} сом", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="⏳ Проверить оплату / Получить номер",
                    callback_data=f"view:{token}",
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


def admin_keyboard(request_id: int, *, signer: TokenSigner) -> InlineKeyboardMarkup:
    token = signer.sign_id("admin", request_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:a:{token}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"admin:r:{token}"),
            ]
        ]
    )

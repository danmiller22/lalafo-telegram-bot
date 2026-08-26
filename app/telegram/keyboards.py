from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.security import TokenSigner
from app.payment_plans import SINGLE_PRICE, WEEK_PRICE


APARTMENT_KEYBOARD_VERSION = 6


def _support_row(support_url: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)]


def apartment_keyboard(
    apartment_id: int, *, signer: TokenSigner, bot_username: str, support_url: str
) -> InlineKeyboardMarkup:
    payment_token = signer.sign_start_id("payment-link", apartment_id)
    bot_url = f"https://t.me/{bot_username.lstrip('@')}"
    private_url = f"{bot_url}?start=pay_{payment_token}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Посмотреть номер",
                    url=private_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Подать заявку на поиск квартиры",
                    url=f"{bot_url}?start=want",
                )
            ],
            _support_row(support_url),
        ]
    )


def payment_keyboard(
    apartment_id: int, *, signer: TokenSigner, payment_url: str, support_url: str
) -> InlineKeyboardMarkup:
    paid_token = signer.sign_id("paid", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Ссылка на оплату", url=payment_url)],
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
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔐 Один номер — {SINGLE_PRICE} сом",
                    callback_data=(
                        f"plan:s:{signer.sign_id('plan-single', apartment_id)}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⭐ Неделя доступа — {WEEK_PRICE} сом",
                    callback_data=f"plan:w:{signer.sign_id('plan-week', apartment_id)}",
                )
            ],
            _support_row(support_url),
        ]
    )


def receipt_payment_keyboard(
    apartment_id: int,
    *,
    current_plan: str,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
) -> InlineKeyboardMarkup:
    if current_plan == "week":
        switch_button = InlineKeyboardButton(
            text=f"🔐 Выбрать один номер — {SINGLE_PRICE} сом",
            callback_data=f"plan:s:{signer.sign_id('plan-single', apartment_id)}",
        )
    else:
        switch_button = InlineKeyboardButton(
            text=f"⭐ Выбрать неделю — {WEEK_PRICE} сом",
            callback_data=f"plan:w:{signer.sign_id('plan-week', apartment_id)}",
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Ссылка на оплату", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил(а)", callback_data="receipt:send"
                )
            ],
            [switch_button],
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
            [InlineKeyboardButton(text="💳 Ссылка на оплату", url=payment_redirect_url)],
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
) -> InlineKeyboardMarkup:
    token = signer.sign_id("view", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Ссылка на оплату", url=payment_url)],
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

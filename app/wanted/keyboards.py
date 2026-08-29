from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import WantedAd
from app.security import TokenSigner


def main_menu_keyboard(support_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Разместить «Ищу квартиру»", callback_data="wanted:new")],
            [InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)],
        ]
    )


def rooms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Студия", callback_data="wanted:room:studio"),
                InlineKeyboardButton(text="1", callback_data="wanted:room:1"),
                InlineKeyboardButton(text="2", callback_data="wanted:room:2"),
            ],
            [
                InlineKeyboardButton(text="3", callback_data="wanted:room:3"),
                InlineKeyboardButton(text="4+", callback_data="wanted:room:4+"),
            ],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="wanted:cancel")],
        ]
    )


def form_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="wanted:cancel")]
        ]
    )


def notes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="wanted:notes:skip")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="wanted:cancel")],
        ]
    )


def contact_keyboard(username: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Использовать @{username}", callback_data="wanted:contact:self"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="wanted:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё верно — к оплате", callback_data="wanted:create")],
            [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="wanted:new")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="wanted:cancel")],
        ]
    )


def wanted_payment_keyboard(
    ad_id: int,
    *,
    signer: TokenSigner,
    payment_url: str,
    support_url: str,
    pending: bool = False,
) -> InlineKeyboardMarkup:
    token = signer.sign_id("wanted-paid", ad_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 100 сом", url=payment_url)],
            [
                InlineKeyboardButton(
                    text="⏳ Оплата проверяется" if pending else "✅ Проверить оплату",
                    callback_data=f"wanted-paid:{token}",
                )
            ],
            [InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)],
        ]
    )


def wanted_admin_keyboard(ad_id: int, *, signer: TokenSigner) -> InlineKeyboardMarkup:
    token = signer.sign_id("wanted-admin", ad_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить и опубликовать",
                    callback_data=f"wanted-admin:a:{token}",
                ),
                InlineKeyboardButton(
                    text="❌ Отказать", callback_data=f"wanted-admin:r:{token}"
                ),
            ]
        ]
    )


def wanted_public_keyboard(ad: WantedAd, *, support_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Telegram rejects tg://user buttons when the customer restricts discovery
    # by user id (BUTTON_USER_PRIVACY_RESTRICTED), which used to abort the whole
    # publication.  The typed contact remains in the card, and a safe clickable
    # button is added only when the customer has a public username.
    if ad.username:
        rows.append([
            InlineKeyboardButton(
                text="💬 Написать арендатору",
                url=f"https://t.me/{ad.username}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🛟 Техподдержка", url=support_url)])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )

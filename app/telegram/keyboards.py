from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.security import TokenSigner


def apartment_keyboard(
    apartment_id: int, *, signer: TokenSigner, support_url: str
) -> InlineKeyboardMarkup:
    token = signer.sign_id("contact", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📞 Получить номер", callback_data=f"contact:{token}")],
            [InlineKeyboardButton(text="💬 Техподдержка", url=support_url)],
        ]
    )


def payment_keyboard(
    apartment_id: int, *, signer: TokenSigner, payment_url: str
) -> InlineKeyboardMarkup:
    token = signer.sign_id("paid", apartment_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{token}")],
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

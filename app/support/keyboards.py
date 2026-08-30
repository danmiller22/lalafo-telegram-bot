from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.security import TokenSigner
from app.support.faq import FAQ_ITEMS


def support_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item.button, callback_data=f"support:faq:{item.key}")]
        for item in FAQ_ITEMS
    ]
    rows.append(
        [InlineKeyboardButton(text="✖️ Закрыть поддержку", callback_data="support:close")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_admin_keyboard(ticket_id: int, *, signer: TokenSigner) -> InlineKeyboardMarkup:
    token = signer.sign_id("support-reply", ticket_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Ответить клиенту",
                    callback_data=f"support:reply:{token}",
                )
            ]
        ]
    )

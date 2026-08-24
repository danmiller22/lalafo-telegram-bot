from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InputMediaPhoto

from app.lalafo.phone import display_phone
from app.models import Apartment
from app.telegram.formatting import format_apartment
from app.telegram.keyboards import private_contact_keyboard

logger = logging.getLogger(__name__)


def format_private_contact(apartment: Apartment) -> str:
    return "\n".join(
        [
            "✅ Оплата подтверждена",
            "",
            format_apartment(apartment),
            "",
            f"📞 Номер собственника: {display_phone(apartment.phone)}",
            "🔒 Этот контакт доступен только вам.",
        ]
    )


async def send_private_contact(
    bot: Bot,
    *,
    user_id: int,
    apartment: Apartment,
    support_url: str,
    max_photos: int = 5,
) -> None:
    text = format_private_contact(apartment)
    reply_markup = private_contact_keyboard(support_url=support_url)
    photo_urls = apartment.photo_urls[: max(1, min(max_photos, 10))]
    if len(photo_urls) == 1:
        try:
            await bot.send_photo(
                user_id,
                photo_urls[0],
                caption=text,
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            logger.warning(
                "Could not deliver private apartment photo; falling back to text: %s",
                type(exc).__name__,
            )
    elif photo_urls:
        try:
            await bot.send_media_group(
                user_id,
                [InputMediaPhoto(media=url) for url in photo_urls],
            )
        except Exception as exc:
            logger.warning(
                "Could not deliver private apartment album; retrying with main photo: %s",
                type(exc).__name__,
            )
            try:
                await bot.send_photo(user_id, photo_urls[0])
            except Exception as photo_exc:
                logger.warning(
                    "Could not deliver private apartment main photo: %s",
                    type(photo_exc).__name__,
                )
    await bot.send_message(user_id, text, reply_markup=reply_markup)

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InputMediaPhoto, URLInputFile

from app.lalafo.phone import display_phone
from app.models import Apartment
from app.telegram.formatting import format_apartment
from app.telegram.keyboards import private_contact_keyboard

logger = logging.getLogger(__name__)
TELEGRAM_ALBUM_LIMIT = 10


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
    # The public card and the paid private copy must contain the same complete
    # photo set. ``max_photos`` remains in the signature for compatibility.
    photo_urls = list(dict.fromkeys(apartment.photo_urls))
    if len(photo_urls) == 1:
        try:
            await bot.send_photo(
                user_id,
                URLInputFile(photo_urls[0], timeout=25),
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
        for offset in range(0, len(photo_urls), TELEGRAM_ALBUM_LIMIT):
            chunk = photo_urls[offset : offset + TELEGRAM_ALBUM_LIMIT]
            try:
                if len(chunk) == 1:
                    await bot.send_photo(
                        user_id, URLInputFile(chunk[0], timeout=25)
                    )
                else:
                    await bot.send_media_group(
                        user_id,
                        [
                            InputMediaPhoto(media=URLInputFile(url, timeout=25))
                            for url in chunk
                        ],
                    )
            except Exception as exc:
                logger.warning(
                    "Could not deliver private apartment photo batch: %s",
                    type(exc).__name__,
                )
    await bot.send_message(user_id, text, reply_markup=reply_markup)

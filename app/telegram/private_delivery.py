from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto, URLInputFile

from app.lalafo.phone import display_phone
from app.models import Apartment
from app.telegram.formatting import (
    format_apartment,
    format_showcase_apartment,
    is_confirmed_owner,
)
from app.telegram.formatting import format_public_apartment
from app.telegram.keyboards import apartment_keyboard
from app.telegram.keyboards import matching_apartment_keyboard
from app.security import TokenSigner

logger = logging.getLogger(__name__)
TELEGRAM_ALBUM_LIMIT = 10


class PrivatePhotoDeliveryError(RuntimeError):
    """A private apartment card must never be delivered without a photo."""


async def _send_required_photos(
    bot: Bot,
    *,
    user_id: int,
    photo_urls: list[str],
) -> None:
    if not photo_urls:
        raise PrivatePhotoDeliveryError("Apartment photos are required")

    delivered = False
    last_error: Exception | None = None
    for offset in range(0, len(photo_urls), TELEGRAM_ALBUM_LIMIT):
        chunk = photo_urls[offset : offset + TELEGRAM_ALBUM_LIMIT]
        try:
            if len(chunk) == 1:
                message = await bot.send_photo(
                    user_id, URLInputFile(chunk[0], timeout=25)
                )
                delivered = delivered or getattr(message, "message_id", None) is not None
            else:
                messages = list(
                    await bot.send_media_group(
                        user_id,
                        [
                            InputMediaPhoto(media=URLInputFile(url, timeout=25))
                            for url in chunk
                        ],
                    )
                )
                delivered = delivered or bool(messages) and all(
                    getattr(message, "message_id", None) is not None
                    for message in messages
                )
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Could not deliver required private apartment photos: %s",
                type(exc).__name__,
            )

    if not delivered:
        raise PrivatePhotoDeliveryError(
            "Telegram did not acknowledge apartment photos"
        ) from last_error


def public_description(value: str | None) -> str:
    """Remove contact routes before showing a source description for free."""
    text = (value or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?<!\d)(?:(?:\+?996|0)[\s()\-]*)?(?:2|5|7|9)\d{2}(?:[\s()\-]*\d){6}(?!\d)",
        "[контакт доступен после оплаты]",
        text,
    )
    return re.sub(
        r"(?:https?://)?t\.me/[A-Za-z0-9_]+|@[A-Za-z0-9_]{4,}",
        "[контакт доступен после оплаты]",
        text,
        flags=re.IGNORECASE,
    )


def format_private_contact(apartment: Apartment) -> str:
    parts = [format_apartment(apartment)]
    parts.extend(("", f"📞 Контакт по объявлению: {display_phone(apartment.phone)}"))
    source_url = (getattr(apartment, "source_url", None) or "").strip()
    if source_url.startswith(("https://", "http://")):
        parts.append(f"🔗 Исходное объявление: {source_url}")
    return "\n".join(parts)


async def send_private_contact(
    bot: Bot,
    *,
    user_id: int,
    apartment: Apartment,
    support_url: str,
    max_photos: int = 5,
) -> None:
    text = format_private_contact(apartment)
    # Never leave a paid private delivery as a photo-less text card.
    # ``max_photos`` remains in the signature for compatibility.
    photo_urls = list(dict.fromkeys(apartment.photo_urls))
    await _send_required_photos(bot, user_id=user_id, photo_urls=photo_urls)
    await bot.send_message(user_id, text)


async def send_private_public_card(
    bot: Bot,
    *,
    user_id: int,
    apartment: Apartment,
    signer: TokenSigner,
    bot_username: str,
    support_url: str,
) -> None:
    """Send a fresh, phone-free public card to one user's private chat.

    Photos are uploaded as new Telegram media and the text is rendered from
    the stored original apartment, so this is never a forwarded group post.
    """
    urls = list(dict.fromkeys(apartment.photo_urls))
    keyboard = apartment_keyboard(
        apartment.id,
        phone=apartment.phone, confirmed_owner=is_confirmed_owner(apartment), signer=signer,
        bot_username=bot_username,
        support_url=support_url,
        include_duplicate=False,
    )
    text = format_public_apartment(apartment, bot_username=bot_username)
    if not urls:
        raise PrivatePhotoDeliveryError("Apartment photos are required")
    if len(urls) == 1:
        try:
            await bot.send_photo(
                user_id,
                URLInputFile(urls[0], timeout=25),
                caption=text,
                reply_markup=keyboard,
            )
            return
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as exc:
            logger.warning("Could not deliver duplicate apartment photo: %s", type(exc).__name__)
    await _send_required_photos(bot, user_id=user_id, photo_urls=urls)
    await bot.send_message(user_id, text, reply_markup=keyboard)


async def send_matching_card(
    bot: Bot,
    *,
    user_id: int,
    apartment: Apartment,
    signer: TokenSigner,
    bot_username: str,
) -> int:
    """Send a phone-free apartment card for hot and personalized feeds."""
    keyboard = matching_apartment_keyboard(
        apartment.id,
        phone=apartment.phone, confirmed_owner=is_confirmed_owner(apartment), signer=signer,
        bot_username=bot_username,
    )
    text = format_showcase_apartment(apartment)
    urls = list(dict.fromkeys(apartment.photo_urls))
    if not urls:
        raise PrivatePhotoDeliveryError("Apartment photos are required")
    if len(urls) == 1 and len(text) <= 1024:
        try:
            message = await bot.send_photo(
                user_id,
                URLInputFile(urls[0], timeout=25),
                caption=text,
                reply_markup=keyboard,
            )
            return message.message_id
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception as exc:
            logger.warning(
                "Could not deliver matching apartment photo: %s",
                type(exc).__name__,
            )
    await _send_required_photos(bot, user_id=user_id, photo_urls=urls)
    message = await bot.send_message(user_id, text, reply_markup=keyboard)
    return message.message_id

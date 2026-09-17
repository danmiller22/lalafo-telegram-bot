from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re

from aiogram import Bot, F, Router
from aiogram.types import Message

from app.config import Settings
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from scripts.publish_inventory import _valid


router = Router(name="admin-lalafo-links")
logger = logging.getLogger(__name__)
_LALAFO_URL = re.compile(
    r"https://(?:www\.)?lalafo\.kg/[^\s<>]+-id-\d+(?:\?[^\s<>]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ").,;!?]}>\"'"
_publish_lock = asyncio.Lock()
REPOST_AFTER = timedelta(hours=48)


def extract_lalafo_url(text: str | None) -> str | None:
    match = _LALAFO_URL.search(text or "")
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) if match else None


def repost_available_at(
    published_at: datetime | None, *, now: datetime | None = None
) -> datetime | None:
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    available_at = published_at.astimezone(timezone.utc) + REPOST_AFTER
    current = now or datetime.now(timezone.utc)
    return available_at if available_at > current else None


@router.message(F.chat.type == "private", F.text)
async def publish_lalafo_link(
    message: Message,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    url = extract_lalafo_url(message.text)
    if url is None:
        return
    if (
        not settings.admin_user_id
        or message.from_user is None
        or message.from_user.id != settings.admin_user_id
    ):
        return

    async with _publish_lock:
        await message.answer("⏳ Проверяю квартиру…")
        try:
            async with LalafoClient(
                timeout=settings.http_timeout_seconds,
                max_retries=settings.http_max_retries,
                proxy_url=settings.lalafo_proxy_url,
            ) as client:
                ad = await client.detail(url)
        except LalafoNotFound:
            await message.answer("❌ Объявление удалено или больше недоступно.")
            return
        except (LalafoError, LalafoParseError, ValueError):
            logger.exception("Admin Lalafo link could not be loaded")
            await message.answer("⚠️ Не удалось загрузить Lalafo. Попробуйте ещё раз.")
            return

        valid, reason = _valid(ad, settings)
        if not valid:
            await message.answer(f"❌ Квартира не прошла фильтр: {reason}.")
            return

        existing = await apartments.get_by_lalafo(ad.lalafo_id)
        available_at = repost_available_at(existing.published_at if existing else None)
        if available_at is not None:
            remaining = available_at - datetime.now(timezone.utc)
            hours = max(1, int(remaining.total_seconds() // 3600) + 1)
            await message.answer(
                f"♻️ Эта квартира уже была в группе. "
                f"Повтор будет доступен примерно через {hours} ч."
            )
            return
        is_repeat = bool(existing and existing.publication_status == "published")
        if not is_repeat and await apartments.is_duplicate(ad):
            await message.answer("♻️ Эта квартира уже опубликована под другой ссылкой.")
            return

        apartment = await apartments.upsert_discovered(ad, discovery_priority=True)
        publisher = TelegramPublisher(
            bot,
            chat_id=settings.telegram_group_id,
            signer=signer,
            bot_username=settings.telegram_bot_username,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )
        try:
            published = await publisher.publish(apartment.id, ad)
        except TelegramPublishError:
            logger.exception("Admin Lalafo card could not be published")
            await message.answer("⚠️ Telegram не принял карточку. Попробуйте ещё раз.")
            return

        # Telegram has already accepted the public post. Never ask the admin to
        # retry just because recording its metadata failed: that would create a
        # duplicate card in the group.
        try:
            await apartments.mark_published(
                apartment.id,
                chat_id=settings.telegram_group_id,
                message_id=published.message_id,
            )
        except Exception:
            logger.exception("Published Lalafo card metadata could not be saved")
            await message.answer(
                "✅ Карточка опубликована, но отметка в базе не сохранилась. "
                "Повторно ссылку сейчас не отправляйте."
            )
            return

        await message.answer(
            f"✅ Квартира опубликована в группе. ID: {ad.lalafo_id}."
        )

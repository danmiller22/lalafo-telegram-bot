from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InputMediaPhoto, Message

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.formatting import format_apartment
from app.telegram.keyboards import apartment_keyboard

logger = logging.getLogger(__name__)


class TelegramPublishError(RuntimeError):
    pass


class TelegramPublisher:
    def __init__(
        self,
        bot: Bot,
        *,
        chat_id: int,
        signer: TokenSigner,
        max_photos: int = 5,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.signer = signer
        self.max_photos = max(1, min(max_photos, 10))

    async def _retry(self, method, *args, **kwargs):
        for attempt in range(4):
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt == 3:
                    raise
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except TelegramNetworkError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)
        raise TelegramPublishError("Telegram retry loop ended unexpectedly")

    async def publish(self, apartment_id: int, ad: LalafoAd) -> Message:
        urls = ad.photo_urls[: self.max_photos]
        if not urls:
            raise TelegramPublishError("Apartment has no photos")
        album_messages: list[Message] = []
        try:
            if len(urls) == 1:
                album_messages = [
                    await self._retry(self.bot.send_photo, chat_id=self.chat_id, photo=urls[0])
                ]
            else:
                media = [InputMediaPhoto(media=url) for url in urls]
                album_messages = list(
                    await self._retry(self.bot.send_media_group, chat_id=self.chat_id, media=media)
                )
        except Exception as album_error:
            logger.warning("Full album failed; retrying with main photo: %s", type(album_error).__name__)
            try:
                main_photo = await self._retry(
                    self.bot.send_photo, chat_id=self.chat_id, photo=urls[0]
                )
                album_messages = [main_photo]
            except Exception as exc:
                raise TelegramPublishError("Telegram could not download apartment photos") from exc
        try:
            return await self._retry(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=format_apartment(ad),
                reply_markup=apartment_keyboard(apartment_id, signer=self.signer),
            )
        except Exception as exc:
            for message in album_messages:
                try:
                    await self.bot.delete_message(self.chat_id, message.message_id)
                except Exception:
                    pass
            raise TelegramPublishError("Telegram apartment card failed") from exc

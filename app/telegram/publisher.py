from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InputMediaPhoto, Message, URLInputFile

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment
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
        bot_username: str,
        support_url: str,
        max_photos: int = 5,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.signer = signer
        self.bot_username = bot_username
        self.support_url = support_url
        self.max_photos = max(1, min(max_photos, 10))

    async def _retry(self, method, *args, **kwargs):
        for attempt in range(5):
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt == 4:
                    raise
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except (TelegramNetworkError, TelegramServerError):
                if attempt == 4:
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
                    await self._retry(
                        self.bot.send_photo,
                        chat_id=self.chat_id,
                        photo=URLInputFile(urls[0], timeout=25),
                    )
                ]
            else:
                media = [
                    InputMediaPhoto(media=URLInputFile(url, timeout=25)) for url in urls
                ]
                album_messages = list(
                    await self._retry(self.bot.send_media_group, chat_id=self.chat_id, media=media)
                )
        except Exception as album_error:
            logger.warning("Full album failed; retrying with main photo: %s", type(album_error).__name__)
            try:
                main_photo = await self._retry(
                    self.bot.send_photo,
                    chat_id=self.chat_id,
                    photo=URLInputFile(urls[0], timeout=25),
                )
                album_messages = [main_photo]
            except Exception as exc:
                raise TelegramPublishError("Telegram could not download apartment photos") from exc
        try:
            return await self._retry(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=format_public_apartment(ad, bot_username=self.bot_username),
                reply_markup=apartment_keyboard(
                    apartment_id,
                    signer=self.signer,
                    bot_username=self.bot_username,
                    support_url=self.support_url,
                ),
            )
        except Exception as exc:
            for message in album_messages:
                try:
                    await self.bot.delete_message(self.chat_id, message.message_id)
                except Exception:
                    pass
            raise TelegramPublishError("Telegram apartment card failed") from exc

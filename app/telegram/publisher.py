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
        self._retry_not_before = 0.0
        self._rate_limit_lock = asyncio.Lock()

    async def _wait_for_shared_rate_limit(self) -> None:
        delay = self._retry_not_before - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _set_shared_rate_limit(self, seconds: float) -> None:
        async with self._rate_limit_lock:
            self._retry_not_before = max(
                self._retry_not_before,
                asyncio.get_running_loop().time() + max(0.5, seconds),
            )

    async def _retry(self, method, *args, **kwargs):
        for attempt in range(5):
            await self._wait_for_shared_rate_limit()
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt == 4:
                    raise
                await self._set_shared_rate_limit(float(exc.retry_after) + 0.5)
            except (TelegramNetworkError, TelegramServerError):
                if attempt == 4:
                    raise
                await asyncio.sleep(min(8.0, 2**attempt + attempt * 0.25))
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
                        photo=urls[0],
                    )
                ]
            else:
                # Telegram downloads public Lalafo images directly. This avoids
                # relaying every byte through the small cloud worker and makes
                # large batches several times faster.
                media = [InputMediaPhoto(media=url) for url in urls]
                album_messages = list(
                    await self._retry(
                        self.bot.send_media_group,
                        chat_id=self.chat_id,
                        media=media,
                    )
                )
        except Exception as album_error:
            logger.warning(
                "Direct Telegram album failed; retrying with main photo: %s",
                type(album_error).__name__,
            )
            try:
                if len(urls) > 1:
                    try:
                        main_photo = await self._retry(
                            self.bot.send_photo,
                            chat_id=self.chat_id,
                            photo=urls[0],
                        )
                    except Exception:
                        main_photo = await self._retry(
                            self.bot.send_photo,
                            chat_id=self.chat_id,
                            photo=URLInputFile(urls[0], timeout=25),
                        )
                else:
                    # The direct URL has already exhausted its retries.
                    # Switch immediately to the streaming fallback.
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

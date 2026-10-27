from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto, Message

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment, is_supported_source
from app.telegram.keyboards import apartment_keyboard

from app.lalafo.exclusions import is_excluded_agency

logger = logging.getLogger(__name__)
TELEGRAM_ALBUM_LIMIT = 10
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 7.0


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
        include_duplicate: bool = False,
        request_timeout_seconds: float = TELEGRAM_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.signer = signer
        self.bot_username = bot_username
        self.support_url = support_url
        self.max_photos = max(1, min(max_photos, 5))
        self.include_duplicate = include_duplicate
        self.request_timeout_seconds = request_timeout_seconds
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
                async with asyncio.timeout(self.request_timeout_seconds):
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

    async def _delete_messages(self, messages: list[Message]) -> None:
        for message in messages:
            message_id = getattr(message, "message_id", None)
            if message_id is None:
                continue
            try:
                async with asyncio.timeout(3.0):
                    await self.bot.delete_message(self.chat_id, message_id)
            except Exception:
                pass

    @staticmethod
    def _photos_acknowledged(messages: list[Message]) -> bool:
        return bool(messages) and all(
            getattr(message, "message_id", None) is not None for message in messages
        )

    async def publish(self, apartment_id: int, ad: LalafoAd, *, reply_markup: InlineKeyboardMarkup | None = None) -> Message:
        if not is_supported_source(ad):
            raise TelegramPublishError("Unsupported apartment source")
        if is_excluded_agency(ad):
            raise TelegramPublishError("Excluded agency source")
        if ad.rooms not in ("studio", "1"):
            raise TelegramPublishError("Only studios and one-room apartments are allowed")
        # A bad remote image can hold Telegram's media fetch open for minutes.
        # A compact single album gives renters enough context and keeps one
        # listing from blocking the publication queue.
        urls = list(dict.fromkeys(ad.photo_urls))[: self.max_photos]
        album_messages: list[Message] = []
        for offset in range(0, len(urls), TELEGRAM_ALBUM_LIMIT):
            chunk = urls[offset : offset + TELEGRAM_ALBUM_LIMIT]
            try:
                if len(chunk) == 1:
                    sent = [
                        await self._retry(
                            self.bot.send_photo,
                            chat_id=self.chat_id,
                            photo=chunk[0],
                        )
                    ]
                else:
                    # Telegram downloads public Lalafo images directly. This
                    # keeps full albums fast on the small cloud worker.
                    sent = list(
                        await self._retry(
                            self.bot.send_media_group,
                            chat_id=self.chat_id,
                            media=[InputMediaPhoto(media=url) for url in chunk],
                        )
                    )
                if not self._photos_acknowledged(sent):
                    raise TelegramPublishError(
                        "Telegram did not acknowledge the apartment photos"
                    )
            except Exception as direct_error:
                # Manually supplied Telegram file IDs are already hosted by
                # Telegram and cannot be downloaded as public URLs.
                if not all(url.startswith(("http://", "https://")) for url in chunk):
                    logger.warning(
                        "Telegram rejected apartment photo file IDs; publishing text: %s",
                        type(direct_error).__name__,
                    )
                    break
                logger.warning(
                    "Direct Telegram photo batch failed; trying photos individually: %s",
                    type(direct_error).__name__,
                )
                sent = []
                last_error: Exception = direct_error
                for url in chunk:
                    try:
                        sent = [
                            await self._retry(
                                self.bot.send_photo,
                                chat_id=self.chat_id,
                                photo=url,
                            )
                        ]
                        if self._photos_acknowledged(sent):
                            break
                        sent = []
                        last_error = TelegramPublishError(
                            "Telegram did not acknowledge the apartment photo"
                        )
                    except Exception as exc:
                        last_error = exc
                if not sent:
                    logger.warning(
                        "Telegram could not publish apartment photos; publishing text: %s",
                        type(last_error).__name__,
                    )
                    break
            album_messages.extend(sent)
        try:
            return await self._retry(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=format_public_apartment(
                    ad,
                    bot_username=self.bot_username,
                    wanted_deep_link=True,
                ),
                parse_mode="HTML",
                reply_markup=reply_markup if reply_markup is not None else apartment_keyboard(
                    apartment_id,
                    signer=self.signer,
                    bot_username=self.bot_username,
                    support_url=self.support_url,
                    include_duplicate=self.include_duplicate,
                ),
            )
        except asyncio.CancelledError:
            await self._delete_messages(album_messages)
            raise
        except Exception as exc:
            await self._delete_messages(album_messages)
            raise TelegramPublishError("Telegram apartment card failed") from exc

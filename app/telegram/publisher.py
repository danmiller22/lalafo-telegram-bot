from __future__ import annotations

import asyncio
import logging

import httpx

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.formatting import is_confirmed_owner, format_public_apartment, is_supported_source
from app.telegram.keyboards import apartment_keyboard

from app.listing_policy import PUBLIC_ROOMS, author_policy_eligible
from app.lalafo.exclusions import is_excluded_agency

logger = logging.getLogger(__name__)
TELEGRAM_ALBUM_LIMIT = 10
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 15.0


class TelegramPublishError(RuntimeError):
    pass


class TelegramPublishAmbiguousError(TelegramPublishError):
    """Telegram may have accepted a send; reconcile before sending it again."""


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
        request_timeout_seconds: float | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.signer = signer
        self.bot_username = bot_username
        self.support_url = support_url
        self.max_photos = max(1, min(max_photos, 5))
        self.include_duplicate = include_duplicate
        self.request_timeout_seconds = (TELEGRAM_REQUEST_TIMEOUT_SECONDS
                                        if request_timeout_seconds is None else request_timeout_seconds)
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

    async def _retry(self, method, *args, is_send: bool = False, **kwargs):
        is_send = is_send or getattr(method, '__name__', '') in {'send_message', 'send_photo', 'send_media_group'}
        for attempt in range(5):
            await self._wait_for_shared_rate_limit()
            try:
                async with asyncio.timeout(self.request_timeout_seconds):
                    return await method(*args, **kwargs)
            except TelegramRetryAfter as exc:
                if attempt == 4:
                    raise
                await self._set_shared_rate_limit(float(exc.retry_after) + 0.5)
            except (TelegramNetworkError, TelegramServerError, TimeoutError) as exc:
                if is_send:
                    raise TelegramPublishAmbiguousError('Telegram send acknowledgement was lost') from exc
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

    async def _fresh_photo_url(self, ad, fallback: str) -> str:
        if not ad.source_url.startswith("https://t.me/"):
            return fallback
        # Public preview image URLs expire. Re-read the same source post;
        # preserve its description/price/contact snapshot and refresh media only.
        from bs4 import BeautifulSoup
        from app.telegram.sources import PHOTO_URL_RE
        post = ad.source_url.removeprefix("https://t.me/")
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                response = await client.get("https://t.me/s/" + post)
                response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for message in soup.select(".tgme_widget_message[data-post]"):
                if message.get("data-post") != post:
                    continue
                wrapper = message.find_parent(class_="tgme_widget_message_wrap") or message
                for photo in wrapper.select(".tgme_widget_message_photo_wrap[style]"):
                    match = PHOTO_URL_RE.search(str(photo.get("style") or ""))
                    if match:
                        return match.group(1)
        except Exception:
            logger.warning("Source photo refresh failed; retaining existing media URL")
        return fallback

    async def _upload_photo(self, url: str):
        # Telegram's URL fetcher cannot always read public CDN URLs. Download
        # a bounded image locally only after a definite API rejection.
        data = bytearray()
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if not response.headers.get("content-type", "").startswith("image/"):
                    raise TelegramPublishError("Photo source is not an image")
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 5 * 1024 * 1024:
                        raise TelegramPublishError("Photo source exceeds upload limit")
        if not data:
            raise TelegramPublishError("Photo source is empty")
        return await self._retry(self.bot.send_photo, is_send=True,
            chat_id=self.chat_id, photo=BufferedInputFile(bytes(data), filename="apartment.jpg"))

    async def publish(self, apartment_id: int, ad: LalafoAd, *, reply_markup: InlineKeyboardMarkup | None = None, archived: bool = False) -> Message:
        if archived:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Актуальные квартиры", url=f"https://t.me/{self.bot_username.lstrip('@')}")
            ]])
        if not is_supported_source(ad):
            raise TelegramPublishError("Unsupported apartment source")
        if not author_policy_eligible(seller_type=ad.seller_type, owner_listing=ad.owner_listing):
            raise TelegramPublishError("inconsistent owner metadata")
        if is_excluded_agency(ad):
            raise TelegramPublishError("Excluded agency source")
        from app.shared_housing import SHARED_ROOMS, valid_shared
        if ad.rooms == SHARED_ROOMS and not valid_shared(ad)[0]:
            raise TelegramPublishError("Invalid shared housing offer")
        if ad.rooms not in (*PUBLIC_ROOMS, SHARED_ROOMS):
            raise TelegramPublishError("Only studios, one-room and two-room apartments are allowed")
        # A bad remote image can hold Telegram's media fetch open for minutes.
        # A compact single album gives renters enough context and keeps one
        # listing from blocking the publication queue.
        urls = list(dict.fromkeys(ad.photo_urls))[: self.max_photos]
        if not urls:
            raise TelegramPublishError("Apartment photos are required")
        album_messages: list[Message] = []
        for offset in range(0, len(urls), TELEGRAM_ALBUM_LIMIT):
            chunk = urls[offset : offset + TELEGRAM_ALBUM_LIMIT]
            try:
                if len(chunk) == 1:
                    sent = [
                        await self._retry(
                            self.bot.send_photo,
                            is_send=True,
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
                            is_send=True,
                            chat_id=self.chat_id,
                            media=[InputMediaPhoto(media=url) for url in chunk],
                        )
                    )
                if not self._photos_acknowledged(sent):
                    raise TelegramPublishError(
                        "Telegram did not acknowledge the apartment photos"
                    )
            except TelegramPublishAmbiguousError:
                raise
            except Exception as direct_error:
                # Manually supplied Telegram file IDs are already hosted by
                # Telegram and cannot be downloaded as public URLs.
                if not all(url.startswith(("http://", "https://")) for url in chunk):
                    logger.warning(
                        "Telegram rejected apartment photo file IDs; skipping card: %s",
                        type(direct_error).__name__,
                    )
                    raise TelegramPublishError(
                        "Telegram could not publish apartment photos"
                    ) from direct_error
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
                                is_send=True,
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
                    except TelegramPublishAmbiguousError:
                        raise
                    except Exception as exc:
                        last_error = exc
                if not sent and isinstance(last_error, TelegramBadRequest):
                    try:
                        photo_url = await self._fresh_photo_url(ad, chunk[0])
                        uploaded = await self._upload_photo(photo_url)
                        sent = [uploaded] if self._photos_acknowledged([uploaded]) else []
                    except TelegramPublishAmbiguousError:
                        raise
                    except Exception as upload_error:
                        last_error = upload_error
                if not sent:
                    logger.warning(
                        "Telegram could not publish apartment photos; skipping card: %s",
                        type(last_error).__name__,
                    )
                    raise TelegramPublishError(
                        "Telegram could not publish apartment photos"
                    ) from last_error
            album_messages.extend(sent)
        if not self._photos_acknowledged(album_messages):
            raise TelegramPublishError("Apartment photos are required")
        try:
            return await self._retry(
                self.bot.send_message,
                is_send=True,
                chat_id=self.chat_id,
                text=format_public_apartment(
                    ad,
                    bot_username=self.bot_username,
                    wanted_deep_link=True,
                    archived=archived,
                ),
                parse_mode="HTML",
                reply_markup=reply_markup if reply_markup is not None else apartment_keyboard(
                    apartment_id,
                    phone=ad.phone, confirmed_owner=is_confirmed_owner(ad), signer=self.signer,
                    bot_username=self.bot_username,
                    support_url=self.support_url,
                    include_duplicate=self.include_duplicate,
                ),
            )
        except asyncio.CancelledError:
            # The caption may already exist. Keep its album for reconciliation.
            raise
        except TelegramPublishAmbiguousError:
            raise
        except Exception as exc:
            await self._delete_messages(album_messages)
            raise TelegramPublishError("Telegram apartment card failed") from exc

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import URLInputFile

from app.config import Settings
from app.favorites import FavoriteRepository
from app.lalafo.managed_ads import (
    LalafoManagedAdsClient,
    ManagedAdsAuthenticationError,
)
from app.models import Apartment
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.formatting import format_showcase_apartment
from app.telegram.keyboards import customer_menu_keyboard, showcase_apartment_keyboard


logger = logging.getLogger(__name__)


class ShowcaseService:
    def __init__(
        self,
        apartments: ApartmentRepository,
        favorites: FavoriteRepository,
        settings: Settings,
        signer: TokenSigner,
    ) -> None:
        self.apartments = apartments
        self.favorites = favorites
        self.settings = settings
        self.signer = signer
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._profile_lock = asyncio.Lock()
        self._client = LalafoManagedAdsClient(
            timeout=getattr(settings, "http_timeout_seconds", 25.0),
            proxy_url=getattr(settings, "lalafo_proxy_url", ""),
        )

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self._client.close()

    def start(self, bot: Bot, user_id: int) -> None:
        previous = self._tasks.get(user_id)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._deliver(bot, user_id), name=f"showcase-{user_id}"
        )
        self._tasks[user_id] = task

    async def _load_current(self) -> list[Apartment]:
        async with self._profile_lock:
            login, password = self.settings.require_lalafo_auto_reply_credentials()
            if self._client.session is None:
                session = await self._client.login(login, password)
            else:
                session = self._client.session
            if session.profile_id != self.settings.lalafo_showcase_profile_id:
                self._client.session = None
                raise RuntimeError("Authenticated Lalafo profile does not match showcase")
            try:
                feed = await self._client.all_my_ads()
            except ManagedAdsAuthenticationError:
                session = await self._client.login(login, password)
                if session.profile_id != self.settings.lalafo_showcase_profile_id:
                    self._client.session = None
                    raise RuntimeError("Authenticated Lalafo profile does not match showcase")
                feed = await self._client.all_my_ads()

        sources = await self.apartments.managed_lalafo_sources(
            deduplicate_sources=False
        )
        by_managed_id = {
            row.managed_lalafo_ad_id: row.apartment
            for row in sources
            if row.managed_lalafo_ad_id is not None
        }
        active: list[tuple[int, Apartment]] = []
        for item in feed:
            try:
                managed_id = int(item["id"])
                status_id = int(item.get("status_id") or 0)
                owner_id = int(
                    item.get("user_id") or self.settings.lalafo_showcase_profile_id
                )
                updated = int(item.get("updated_time") or item.get("created_time") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            apartment = by_managed_id.get(managed_id)
            if (
                status_id == 2
                and owner_id == self.settings.lalafo_showcase_profile_id
                and apartment is not None
                and apartment.phone
                and apartment.active
                and apartment.availability_status != "unavailable"
            ):
                active.append((updated, apartment))
        active.sort(key=lambda value: (value[0], value[1].id), reverse=True)
        return [apartment for _, apartment in active]

    async def current_apartment_ids(self) -> set[int]:
        return {apartment.id for apartment in await self._load_current()}

    async def _send_card(
        self, bot: Bot, user_id: int, apartment: Apartment, *, saved: bool
    ) -> None:
        text = format_showcase_apartment(apartment)
        keyboard = showcase_apartment_keyboard(
            apartment.id, signer=self.signer, saved=saved
        )
        photo = next(iter(dict.fromkeys(apartment.photo_urls)), None)
        if photo:
            try:
                await bot.send_photo(
                    user_id,
                    URLInputFile(photo, timeout=25),
                    caption=text,
                    reply_markup=keyboard,
                )
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after))
                await bot.send_photo(
                    user_id,
                    URLInputFile(photo, timeout=25),
                    caption=text,
                    reply_markup=keyboard,
                )
                return
            except TelegramForbiddenError:
                raise
            except Exception:
                logger.warning("Could not send showcase photo", exc_info=True)
        await bot.send_message(user_id, text, reply_markup=keyboard)

    async def _deliver(self, bot: Bot, user_id: int) -> None:
        task = asyncio.current_task()
        try:
            try:
                apartments = await self._load_current()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not load live showcase")
                await bot.send_message(
                    user_id,
                    "Не удалось загрузить квартиры. Попробуйте ещё раз позже.",
                )
                await bot.send_message(
                    user_id, "Выберите действие:", reply_markup=customer_menu_keyboard()
                )
                return
            if not apartments:
                await bot.send_message(user_id, "Сейчас нет доступных квартир.")
                await bot.send_message(
                    user_id, "Выберите действие:", reply_markup=customer_menu_keyboard()
                )
                return
            saved_ids = await self.favorites.saved_ids(user_id)
            interval = max(0.0, self.settings.showcase_card_interval_seconds)
            for apartment in apartments:
                await self._send_card(
                    bot, user_id, apartment, saved=apartment.id in saved_ids
                )
                # Keep the Telegram API calls sequential. This pause happens
                # only after a card has actually been accepted by Telegram.
                await asyncio.sleep(interval)
            await bot.send_message(
                user_id, "Выберите действие:", reply_markup=customer_menu_keyboard()
            )
        except TelegramForbiddenError:
            logger.info("Customer blocked bot during showcase delivery")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Showcase delivery failed")
        finally:
            if self._tasks.get(user_id) is task:
                self._tasks.pop(user_id, None)

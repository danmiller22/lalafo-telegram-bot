from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import URLInputFile

from app.config import Settings
from app.favorites import FavoriteRepository
from app.matching.repository import MatchingRepository
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
        matching: MatchingRepository,
        favorites: FavoriteRepository,
        settings: Settings,
        signer: TokenSigner,
    ) -> None:
        self.apartments = apartments
        self.matching = matching
        self.favorites = favorites
        self.settings = settings
        self.signer = signer
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def start(self, bot: Bot, user_id: int) -> None:
        previous = self._tasks.get(user_id)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._deliver(bot, user_id), name=f"showcase-{user_id}"
        )
        self._tasks[user_id] = task

    async def _load_current(self) -> list[Apartment]:
        # The database is continuously refreshed by the independent Lalafo and
        # Telegram collectors. Reading it here avoids a customer-facing outage
        # when Lalafo rejects the Koyeb egress IP and includes every configured
        # source instead of only the managed profile mirror.
        return await self.matching.current_apartments(limit=500)

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

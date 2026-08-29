from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot import admin, handlers
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.wanted import admin as wanted_admin
from app.wanted import handlers as wanted_handlers
from app.wanted.repository import WantedAdRepository


@dataclass(slots=True)
class BotRuntime:
    bot: Bot
    dispatcher: Dispatcher
    engine: AsyncEngine
    workflow_data: dict[str, Any]

    async def close(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()


async def create_runtime() -> BotRuntime:
    settings = get_settings()
    signer = TokenSigner(settings.require_callback_secret())
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    payments = PaymentRepository(sessions)
    wanted_ads = WantedAdRepository(sessions)
    service = PaymentService(apartments, payments, admin_user_id=settings.admin_user_id)
    bot = Bot(token=settings.require_bot_token())
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(wanted_admin.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(wanted_handlers.router)
    dispatcher.include_router(handlers.router)
    workflow_data = {
        "settings": settings,
        "signer": signer,
        "apartments": apartments,
        "payments": payments,
        "service": service,
        "wanted_ads": wanted_ads,
    }
    return BotRuntime(bot, dispatcher, engine, workflow_data)


async def configure_bot_profile(runtime: BotRuntime) -> None:
    """Apply non-critical Telegram metadata after the webhook is already usable.

    Telegram profile calls occasionally take tens of seconds from a cloud IP.
    They must never delay FastAPI readiness or customer messages.
    """
    me = await runtime.bot.get_me()
    await runtime.bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="want", description="Разместить «Ищу квартиру»"),
            BotCommand(command="mywanted", description="Мои заявки"),
            BotCommand(command="status", description="Проверить работу бота"),
        ]
    )
    logging.getLogger(__name__).info("Bot initialized: @%s (id=%s)", me.username, me.id)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = await create_runtime()
    try:
        await runtime.dispatcher.start_polling(
            runtime.bot,
            **runtime.workflow_data,
        )
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(run())

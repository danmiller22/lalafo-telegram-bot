from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot import admin, handlers
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner


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
    service = PaymentService(apartments, payments, admin_user_id=settings.admin_user_id)
    bot = Bot(token=settings.require_bot_token())
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.router)
    dispatcher.include_router(handlers.router)
    workflow_data = {
        "settings": settings,
        "signer": signer,
        "apartments": apartments,
        "payments": payments,
        "service": service,
    }
    try:
        me = await bot.get_me()
        logging.getLogger(__name__).info("Bot initialized: @%s (id=%s)", me.username, me.id)
    except Exception:
        await bot.session.close()
        await engine.dispose()
        raise
    return BotRuntime(bot, dispatcher, engine, workflow_data)


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

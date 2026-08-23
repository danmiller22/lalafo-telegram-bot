from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot import admin, handlers
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = settings.require_bot_token()
    signer = TokenSigner(settings.require_callback_secret())
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    payments = PaymentRepository(sessions)
    service = PaymentService(apartments, payments, admin_user_id=settings.admin_user_id)
    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.router)
    dispatcher.include_router(handlers.router)
    try:
        me = await bot.get_me()
        logging.getLogger(__name__).info("Bot initialized: @%s (id=%s)", me.username, me.id)
        await dispatcher.start_polling(
            bot,
            settings=settings,
            signer=signer,
            apartments=apartments,
            payments=payments,
            service=service,
        )
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())

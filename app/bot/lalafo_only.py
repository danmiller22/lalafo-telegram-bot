"""Standalone Telegram worker for manual Lalafo publishing.

Run it as a separate Koyeb service with the same database and a dedicated
TELEGRAM_BOT_TOKEN. The token is intentionally read only from the environment.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import lalafo_links
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signer = TokenSigner(settings.require_callback_secret())
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    token = settings.require_lalafo_bot_token()
    if token == settings.telegram_bot_token or token.split(":", 1)[0] == "8867149259":
        raise RuntimeError("Payment bot token is forbidden in the manual worker")
    bot = Bot(token=token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(lalafo_links.owner_router)
    dispatcher.include_router(lalafo_links.router)

    try:
        me = await bot.get_me()
        if me.username != "personn22bot":
            raise RuntimeError("Expected @personn22bot; refusing to change another bot webhook")
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            settings=settings,
            apartments=apartments,
            signer=signer,
        )
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

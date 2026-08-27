from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.featured import admin
from app.featured.repository import FeaturedRepository


@dataclass(slots=True)
class FeaturedReviewRuntime:
    bot: Bot
    dispatcher: Dispatcher
    engine: AsyncEngine
    workflow_data: dict[str, Any]

    async def close(self) -> None:
        await self.bot.session.close()
        await self.engine.dispose()


async def create_featured_review_runtime() -> FeaturedReviewRuntime:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(admin.router)
    bot = Bot(token=settings.require_featured_review_bot_token())
    return FeaturedReviewRuntime(
        bot=bot, dispatcher=dispatcher, engine=engine,
        workflow_data={
            "settings": settings,
            "featured": FeaturedRepository(sessions),
        },
    )

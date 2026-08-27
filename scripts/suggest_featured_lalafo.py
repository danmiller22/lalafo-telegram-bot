from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.featured.repository import FeaturedRepository
from app.featured.selection import build_description, select_featured
from app.payments.repository import ApartmentRepository
from scripts.publish_featured_lalafo import discover


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    if not settings.dry_run and not settings.featured_lalafo_enabled:
        logging.info("Featured suggestions disabled; no-op")
        return 0
    business_date = datetime.now(ZoneInfo(settings.featured_timezone)).date()
    discovered = await discover(settings)
    if settings.dry_run:
        candidates = select_featured(
            discovered, count=settings.featured_max_candidates,
            priority_price=settings.featured_priority_price,
            max_price=settings.featured_max_price,
        )
        for rank, ad in enumerate(candidates, 1):
            logging.info("DRY suggestion rank=%s id=%s url=%s\n%s", rank, ad.lalafo_id, ad.source_url, build_description(ad))
        return 0
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    repo = FeaturedRepository(sessions)
    recent = await repo.recent_source_ids(business_date, settings.featured_cooldown_days)
    candidates = select_featured(
        discovered, count=settings.featured_max_candidates,
        priority_price=settings.featured_priority_price,
        max_price=settings.featured_max_price, recent_source_ids=recent,
    )
    bot = Bot(token=settings.require_featured_review_bot_token())
    try:
        await bot.send_message(
            settings.admin_user_id,
            f"🏠 Подборка на {business_date}. Выберите две квартиры. "
            "Если ничего не подходит — отправьте мне ссылку Lalafo.",
        )
        for rank, ad in enumerate(candidates, 1):
            apartment = await apartments.upsert_discovered(ad)
            candidate = await repo.add_candidate(
                business_date, apartment_id=apartment.id, lalafo_id=ad.lalafo_id,
                source_url=ad.source_url, rank=rank,
            )
            if candidate.telegram_message_id is not None:
                continue
            sent = await bot.send_message(
                settings.admin_user_id,
                f"{rank}. {build_description(ad)}\n\n🔗 {ad.source_url}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="✅ Выбрать эту квартиру",
                    callback_data=f"featured:select:{candidate.id}",
                )]]),
                disable_web_page_preview=False,
            )
            await repo.mark_candidate_message(candidate.id, sent.message_id)
    finally:
        await bot.session.close()
        await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

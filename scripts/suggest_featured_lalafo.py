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
    repo = FeaturedRepository(sessions)
    recent = await repo.recent_source_ids(business_date, settings.featured_cooldown_days)
    candidates = select_featured(
        discovered, count=settings.featured_max_candidates,
        priority_price=settings.featured_priority_price,
        max_price=settings.featured_max_price, recent_source_ids=recent,
    )
    bot = Bot(token=settings.require_featured_review_bot_token())
    try:
        mode_text = (
            "Бот сам выберет две лучшие квартиры и сразу запустит публикацию."
            if settings.featured_auto_select_enabled
            else "Выберите две квартиры. Если ничего не подходит — отправьте мне ссылку Lalafo."
        )
        await bot.send_message(
            settings.admin_user_id,
            f"🏠 Подборка на {business_date}. {mode_text}",
        )
        auto_selected = 0
        for rank, ad in enumerate(candidates, 1):
            candidate = await repo.add_candidate(
                business_date, apartment_id=None, lalafo_id=ad.lalafo_id,
                source_url=ad.source_url,
                source_payload=ad.model_dump(mode="json"), rank=rank,
            )
            automatically_selected = False
            if settings.featured_auto_select_enabled and rank <= settings.featured_count:
                outcome, selected = await repo.select_candidate(
                    candidate.id, limit=settings.featured_count
                )
                if outcome in {"selected", "already_selected"} and selected is not None:
                    approval, _ = await repo.approve_candidate(selected.id)
                    automatically_selected = approval in {"approved", "already_approved"}
                    if automatically_selected:
                        auto_selected += 1
            if candidate.telegram_message_id is not None:
                continue
            keyboard = None
            if not settings.featured_auto_select_enabled:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="✅ Выбрать эту квартиру",
                    callback_data=f"featured:select:{candidate.id}",
                )]])
            prefix = "🤖 Выбрано автоматически\n" if automatically_selected else ""
            sent = await bot.send_message(
                settings.admin_user_id,
                f"{prefix}{rank}. {build_description(ad)}\n\n🔗 {ad.source_url}",
                reply_markup=keyboard,
                disable_web_page_preview=False,
            )
            await repo.mark_candidate_message(candidate.id, sent.message_id)
        if settings.featured_auto_select_enabled:
            await bot.send_message(
                settings.admin_user_id,
                f"✅ Автоматически подготовлено к публикации: {auto_selected} из "
                f"{settings.featured_count}.",
            )
    finally:
        await bot.session.close()
        await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

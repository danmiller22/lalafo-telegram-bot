from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.featured.repository import FeaturedRepository
from app.lalafo.client import LalafoClient
from app.lalafo.parser import is_allowed

router = Router(name="featured-admin")
LALAFO_URL = re.compile(r"https?://(?:www\.)?lalafo\.kg/\S+", re.IGNORECASE)


def _is_admin(user_id: int | None, settings: Settings) -> bool:
    return bool(user_id and settings.admin_user_id and user_id == settings.admin_user_id)


@router.message(CommandStart(), F.chat.type == "private")
async def review_start(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings):
        return
    await message.answer(
        "✅ Бот отбора квартир готов.\n\n"
        "Каждое утро он пришлёт лучшие варианты до 30 000 сом. "
        "Выберите ровно две квартиры или отправьте ему ссылку Lalafo."
    )


@router.callback_query(F.data.startswith("featured:select:"))
async def select_featured(
    callback: CallbackQuery, settings: Settings,
    featured: FeaturedRepository,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        candidate_id = int((callback.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный вариант", show_alert=True)
        return
    outcome, candidate = await featured.select_candidate(
        candidate_id, limit=settings.featured_count
    )
    messages = {
        "selected": "Выбрано. Квартира добавлена в очередь.",
        "already_selected": "Эта квартира уже выбрана.",
        "full": "Уже выбраны две квартиры на сегодня.",
        "missing": "Вариант больше недоступен.",
    }
    await callback.answer(messages[outcome], show_alert=outcome in {"full", "missing"})
    if outcome == "selected" and callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Выбран вариант {candidate.selected_slot}/{settings.featured_count}. "
            "Публикация будет обработана отдельным облачным запуском."
        )


@router.message(F.chat.type == "private", F.text.contains("lalafo.kg/"))
async def accept_custom_lalafo_link(
    message: Message, bot: Bot, settings: Settings,
    featured: FeaturedRepository,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings):
        return
    match = LALAFO_URL.search(message.text or "")
    if not match:
        return
    url = match.group(0).rstrip(").,]")
    await message.answer("Проверяю ссылку Lalafo…")
    try:
        async with LalafoClient(
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            proxy_url=settings.lalafo_proxy_url,
        ) as client:
            ad = await client.detail(url)
        allowed, reason = is_allowed(
            ad, city=settings.city, max_price=settings.featured_max_price,
            rooms=settings.allowed_rooms,
        )
        if not allowed or not ad.district or not ad.phone or not ad.photo_urls:
            await message.answer(f"Не могу принять эту квартиру: {reason or 'неполные данные' }.")
            return
        business_date = datetime.now(ZoneInfo(settings.featured_timezone)).date()
        candidate = await featured.add_candidate(
            business_date, apartment_id=None, lalafo_id=ad.lalafo_id,
            source_url=ad.source_url, source_payload=ad.model_dump(mode="json"), rank=0,
        )
        outcome, selected = await featured.select_candidate(
            candidate.id, limit=settings.featured_count
        )
        if outcome == "full":
            await message.answer("На сегодня уже выбраны две квартиры.")
        elif outcome in {"selected", "already_selected"}:
            await message.answer(
                f"✅ Ссылка принята. Вариант {selected.selected_slot}/{settings.featured_count} "
                "добавлен в очередь облачной публикации."
            )
    except Exception as exc:
        await message.answer(f"Не удалось проверить ссылку: {type(exc).__name__}.")

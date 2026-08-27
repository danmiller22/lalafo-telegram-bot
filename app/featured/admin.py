from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.featured.posting import posting_preview
from app.featured.repository import FeaturedRepository
from app.lalafo.client import LalafoClient
from app.lalafo.models import LalafoAd
from app.lalafo.parser import is_allowed

router = Router(name="featured-admin")
LALAFO_URL = re.compile(r"https?://(?:www\.)?lalafo\.kg/\S+", re.IGNORECASE)


def _is_admin(user_id: int | None, settings: Settings) -> bool:
    return bool(user_id and settings.admin_user_id and user_id == settings.admin_user_id)


def _preview_keyboard(candidate_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить эту публикацию",
            callback_data=f"featured:approve:{candidate_id}",
        )],
        [InlineKeyboardButton(
            text="❌ Не публиковать",
            callback_data=f"featured:reject:{candidate_id}",
        )],
    ])


async def _send_preview(message: Message, candidate_id: int, ad: LalafoAd) -> None:
    await message.answer(
        posting_preview(ad), parse_mode="HTML",
        reply_markup=_preview_keyboard(candidate_id),
    )


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
        ad = LalafoAd.model_validate(candidate.source_payload)
        await callback.message.answer(
            f"✅ Выбран вариант {candidate.selected_slot}/{settings.featured_count}. "
            "Черновик Lalafo подготовлен. Проверьте его ниже."
        )
        await _send_preview(callback.message, candidate.id, ad)


@router.callback_query(F.data.startswith("featured:approve:"))
async def approve_featured(
    callback: CallbackQuery, settings: Settings, featured: FeaturedRepository,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        candidate_id = int((callback.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный вариант", show_alert=True)
        return
    outcome, candidate = await featured.approve_candidate(candidate_id)
    messages = {
        "approved": "Публикация подтверждена.",
        "already_approved": "Эта публикация уже подтверждена.",
        "not_selected": "Сначала выберите квартиру заново.",
        "missing": "Вариант больше недоступен.",
    }
    await callback.answer(messages[outcome], show_alert=outcome not in {"approved", "already_approved"})
    if outcome == "approved" and callback.message and candidate:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ Публикация {candidate.selected_slot}/{settings.featured_count} подтверждена. "
            "Она готова к отдельному безопасному запуску."
        )


@router.callback_query(F.data.startswith("featured:reject:"))
async def reject_featured(
    callback: CallbackQuery, settings: Settings, featured: FeaturedRepository,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        candidate_id = int((callback.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный вариант", show_alert=True)
        return
    outcome, _ = await featured.reject_candidate(candidate_id)
    await callback.answer("Убрано из публикации" if outcome == "rejected" else "Вариант уже недоступен")
    if outcome == "rejected" and callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("❌ Квартира убрана. Можете выбрать другую или прислать новую ссылку.")


@router.message(
    F.chat.type == "private",
    F.text.contains("lalafo.kg/") | F.caption.contains("lalafo.kg/"),
)
async def accept_custom_lalafo_link(
    message: Message, bot: Bot, settings: Settings,
    featured: FeaturedRepository,
) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, settings):
        return
    match = LALAFO_URL.search(message.text or message.caption or "")
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
        outcome, selected = await featured.select_custom_candidate(
            candidate.id, limit=settings.featured_count
        )
        if outcome in {"selected", "replaced", "already_selected"}:
            await message.answer(
                f"✅ Ссылка принята. Квартира выбрана как вариант "
                f"{selected.selected_slot}/{settings.featured_count}. "
                + ("Предыдущий вариант в этом слоте заменён. " if outcome == "replaced" else "")
                + "Все поля Lalafo заполнены в черновике."
            )
            await _send_preview(message, selected.id, ad)
    except Exception as exc:
        await message.answer(f"Не удалось проверить ссылку: {type(exc).__name__}.")

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.support.faq import FAQ_BY_KEY, fallback_answer, faq_for_text
from app.support.keyboards import support_menu_keyboard
from app.support.states import SupportConversation
from app.wanted.keyboards import main_menu_keyboard

router = Router(name="support")


async def begin_support(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SupportConversation.active)
    await message.answer(
        "🛟 Помощь\n\nВыберите вопрос кнопкой или напишите его своими словами — "
        "бот ответит сразу.",
        reply_markup=support_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:support")
async def support_button(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат бота.", show_alert=True)
        return
    await callback.answer()
    await begin_support(callback.message, state)


@router.callback_query(F.data.startswith("support:faq:"))
async def support_faq_callback(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").rsplit(":", 1)[-1]
    item = FAQ_BY_KEY.get(key)
    if item is None:
        await callback.answer("Выберите вопрос кнопкой ниже.", show_alert=True)
        return
    await state.set_state(SupportConversation.active)
    await callback.answer()
    if callback.message:
        await callback.message.answer(item.answer, reply_markup=support_menu_keyboard())


@router.callback_query(F.data == "support:close")
async def support_close(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    await state.clear()
    await callback.answer("Помощь закрыта")
    if callback.message:
        await callback.message.answer(
            "Главное меню:",
            reply_markup=main_menu_keyboard(settings.support_bot_url),
        )


@router.message(StateFilter(SupportConversation.active), F.text)
async def support_question(message: Message) -> None:
    item = faq_for_text((message.text or "").strip())
    answer = item.answer if item is not None else fallback_answer(message.text or "")
    await message.answer(answer, reply_markup=support_menu_keyboard())

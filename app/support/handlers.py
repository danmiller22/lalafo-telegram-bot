from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.security import TokenSigner
from app.support.faq import FAQ_BY_KEY
from app.support.keyboards import (
    support_admin_keyboard,
    support_back_keyboard,
    support_menu_keyboard,
)
from app.support.repository import SupportTicketRepository
from app.support.states import SupportAdminReply, SupportConversation
from app.wanted.keyboards import main_menu_keyboard

router = Router(name="support")
logger = logging.getLogger(__name__)

SUPPORT_MENU_TEXT = (
    "🛟 Помощь\n\nВыберите нужный вопрос кнопкой ниже."
)
SUPPORT_BUTTONS_ONLY_TEXT = (
    "Ничего писать не нужно. Выберите нужный раздел кнопкой ниже."
)


async def begin_support(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SupportConversation.active)
    await message.answer(
        SUPPORT_MENU_TEXT,
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
        await callback.message.edit_text(
            item.answer,
            reply_markup=support_back_keyboard(),
        )


@router.callback_query(F.data == "support:back")
async def support_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportConversation.active)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            SUPPORT_MENU_TEXT,
            reply_markup=support_menu_keyboard(),
        )


@router.callback_query(F.data == "support:operator")
async def support_operator(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportConversation.writing)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "👩‍💻 Подключаем оператора. Напишите ваш вопрос одним сообщением.",
            reply_markup=support_back_keyboard(),
        )


@router.callback_query(F.data == "support:close")
async def support_close(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    await state.clear()
    await callback.answer("Помощь закрыта")
    if callback.message:
        await callback.message.edit_text(
            "Главное меню:",
            reply_markup=main_menu_keyboard(
                settings.support_bot_url,
                include_admin=bool(
                    settings.admin_user_id
                    and callback.from_user.id == settings.admin_user_id
                ),
            ),
        )


@router.message(StateFilter(SupportConversation.active), F.text)
async def support_question(message: Message) -> None:
    await message.answer(
        SUPPORT_BUTTONS_ONLY_TEXT,
        reply_markup=support_menu_keyboard(),
    )


@router.message(StateFilter(SupportConversation.writing), F.text)
async def submit_operator_question(
    message: Message,
    state: FSMContext,
    settings: Settings,
    signer: TokenSigner,
    support_tickets: SupportTicketRepository,
    bot: Bot,
) -> None:
    if not message.from_user or not message.text:
        return
    question = message.text.strip()
    if not 2 <= len(question) <= 3000:
        await message.answer("Сообщение должно содержать от 2 до 3000 символов.")
        return
    if not settings.admin_user_id:
        await message.answer(
            "Связь с оператором временно недоступна.",
            reply_markup=support_menu_keyboard(),
        )
        await state.set_state(SupportConversation.active)
        return
    ticket = await support_tickets.create(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        question=question,
    )
    username = f"@{ticket.username}" if ticket.username else "без username"
    admin_text = (
        f"🛟 Обращение #{ticket.id}\n"
        f"Клиент: {ticket.first_name or 'Имя не указано'} — {username}\n"
        f"Telegram ID: {ticket.telegram_user_id}\n\n"
        f"{ticket.question}"
    )
    try:
        sent = await bot.send_message(
            settings.admin_user_id,
            admin_text,
            reply_markup=support_admin_keyboard(ticket.id, signer=signer),
        )
        await support_tickets.mark_notified(ticket.id, sent.message_id)
    except Exception:
        logger.exception("Could not deliver support ticket %s to admin", ticket.id)
        await message.answer(
            "Не удалось связаться с оператором. Попробуйте ещё раз позже.",
            reply_markup=support_menu_keyboard(),
        )
        await state.set_state(SupportConversation.active)
        return
    await state.set_state(SupportConversation.active)
    await message.answer(
        f"✅ Сообщение передано оператору. Номер обращения: #{ticket.id}.",
        reply_markup=support_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("support:reply:"))
async def begin_operator_reply(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    signer: TokenSigner,
    support_tickets: SupportTicketRepository,
) -> None:
    if not settings.admin_user_id or callback.from_user.id != settings.admin_user_id:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    token = (callback.data or "").removeprefix("support:reply:")
    ticket_id = signer.verify_id("support-reply", token)
    ticket = await support_tickets.get(ticket_id) if ticket_id is not None else None
    if ticket is None or ticket.status != "open":
        await callback.answer("Обращение уже обработано или недоступно.", show_alert=True)
        return
    await state.set_state(SupportAdminReply.writing)
    await state.update_data(support_ticket_id=ticket.id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Напишите ответ клиенту по обращению #{ticket.id} одним сообщением."
        )


@router.message(StateFilter(SupportAdminReply.writing), F.text)
async def send_operator_reply(
    message: Message,
    state: FSMContext,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    bot: Bot,
) -> None:
    if not message.from_user or message.from_user.id != settings.admin_user_id:
        return
    answer = (message.text or "").strip()
    if not 1 <= len(answer) <= 3000:
        await message.answer("Ответ должен содержать от 1 до 3000 символов.")
        return
    data = await state.get_data()
    ticket_id = data.get("support_ticket_id")
    ticket = await support_tickets.get(ticket_id) if isinstance(ticket_id, int) else None
    if ticket is None or ticket.status != "open":
        await state.clear()
        await message.answer("Обращение уже обработано или недоступно.")
        return
    try:
        await bot.send_message(
            ticket.telegram_user_id,
            f"👩‍💻 Ответ оператора по обращению #{ticket.id}:\n\n{answer}",
        )
    except Exception:
        logger.exception("Could not deliver support answer for ticket %s", ticket.id)
        await message.answer("Не удалось доставить ответ клиенту. Попробуйте ещё раз.")
        return
    if not await support_tickets.answer(
        ticket.id, text=answer, actor_id=message.from_user.id
    ):
        await state.clear()
        await message.answer("Обращение уже обработано.")
        return
    await state.clear()
    await message.answer(f"✅ Ответ по обращению #{ticket.id} доставлен клиенту.")

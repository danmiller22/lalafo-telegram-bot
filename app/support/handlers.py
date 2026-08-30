from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.security import TokenSigner
from app.support.faq import FAQ_BY_KEY, faq_for_text
from app.support.keyboards import support_admin_keyboard, support_menu_keyboard
from app.support.repository import SupportTicketRepository
from app.support.states import SupportAdminReply, SupportConversation
from app.wanted.keyboards import main_menu_keyboard

logger = logging.getLogger(__name__)
router = Router(name="support")


def _is_admin(user_id: int, settings: Settings) -> bool:
    return bool(settings.admin_user_id and user_id == settings.admin_user_id)


def _customer_label(message: Message) -> str:
    user = message.from_user
    if user.username:
        return f"@{user.username} · ID {user.id}"
    name = (user.first_name or "Клиент").strip()
    return f"{name} · ID {user.id}"


async def begin_support(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SupportConversation.active)
    await message.answer(
        "🛟 Техподдержка\n\n"
        "Выберите частый вопрос или напишите свой. Если готового ответа не "
        "будет, бот сразу передаст сообщение администратору.",
        reply_markup=support_menu_keyboard(),
    )


@router.message(Command("support"))
async def support_command(
    message: Message,
    state: FSMContext,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    signer: TokenSigner,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        await begin_support(message, state)
        return
    rows = await support_tickets.open_tickets(limit=20)
    if not rows:
        await message.answer("🛟 Нет открытых вопросов клиентов.")
        return
    await message.answer(f"🛟 Открытых вопросов: {len(rows)}")
    for row in rows:
        label = f"@{row.username}" if row.username else str(row.telegram_user_id)
        await message.answer(
            f"Вопрос #{row.id} · {label}\n\n{row.question[:3000]}",
            reply_markup=support_admin_keyboard(row.id, signer=signer),
        )


@router.callback_query(F.data.startswith("support:faq:"))
async def support_faq_callback(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").rsplit(":", 1)[-1]
    item = FAQ_BY_KEY.get(key)
    if item is None:
        await callback.answer("Ответ не найден.", show_alert=True)
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
    await callback.answer("Поддержка закрыта")
    if callback.message:
        await callback.message.answer(
            "Главное меню:",
            reply_markup=main_menu_keyboard(settings.support_bot_url),
        )


async def _handoff_to_admin(
    message: Message,
    *,
    question: str,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    if not settings.admin_user_id:
        await message.answer(
            "Сейчас не удалось подключить администратора. Попробуйте немного позже."
        )
        return
    ticket = await support_tickets.create(
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        question=question[:4000],
    )
    try:
        admin_message = await bot.send_message(
            settings.admin_user_id,
            f"🛟 Новый вопрос #{ticket.id}\n"
            f"Клиент: {_customer_label(message)}\n\n"
            f"{question[:3200]}",
            reply_markup=support_admin_keyboard(ticket.id, signer=signer),
        )
        await support_tickets.mark_notified(ticket.id, admin_message.message_id)
        if not message.text:
            await bot.copy_message(
                settings.admin_user_id,
                message.chat.id,
                message.message_id,
            )
    except Exception:
        logger.exception("Could not notify administrator about support ticket %s", ticket.id)
        await message.answer(
            "Вопрос сохранён, но администратор временно недоступен. "
            "Он увидит обращение в списке /support."
        )
        return
    await message.answer(
        "✅ Готового ответа не нашлось — вопрос передан администратору.\n\n"
        "Ответ придёт сюда от имени этого бота. Можно не дублировать сообщение."
    )


@router.message(StateFilter(SupportConversation.active), F.text)
async def support_question(
    message: Message,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    question = (message.text or "").strip()
    item = faq_for_text(question)
    if item is not None:
        await message.answer(item.answer, reply_markup=support_menu_keyboard())
        return
    await _handoff_to_admin(
        message,
        question=question,
        settings=settings,
        support_tickets=support_tickets,
        signer=signer,
        bot=bot,
    )


@router.message(StateFilter(SupportConversation.active))
async def support_attachment(
    message: Message,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    question = (message.caption or "").strip() or "Клиент прислал вложение в поддержку."
    await _handoff_to_admin(
        message,
        question=question,
        settings=settings,
        support_tickets=support_tickets,
        signer=signer,
        bot=bot,
    )


@router.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    signer: TokenSigner,
    support_tickets: SupportTicketRepository,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    token = (callback.data or "")[len("support:reply:") :]
    ticket_id = signer.verify_id("support-reply", token)
    ticket = await support_tickets.get(ticket_id) if ticket_id is not None else None
    if ticket is None or ticket.status != "open":
        await callback.answer("Вопрос уже закрыт или не найден.", show_alert=True)
        return
    await state.clear()
    await state.set_state(SupportAdminReply.writing)
    await state.update_data(support_ticket_id=ticket.id)
    await callback.answer("Напишите ответ следующим сообщением")
    if callback.message:
        await callback.message.answer(
            f"✍️ Ответ для клиента по вопросу #{ticket.id}.\n"
            "Отправьте текст или вложение следующим сообщением. /cancel — отмена."
        )


@router.message(StateFilter(SupportAdminReply.writing), Command("cancel"))
async def support_reply_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ответ отменён. Вопрос остался открытым.")


@router.message(StateFilter(SupportAdminReply.writing))
async def support_reply_send(
    message: Message,
    state: FSMContext,
    settings: Settings,
    support_tickets: SupportTicketRepository,
    bot: Bot,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    data = await state.get_data()
    ticket_id = int(data.get("support_ticket_id") or 0)
    ticket = await support_tickets.get(ticket_id)
    if ticket is None or ticket.status != "open":
        await state.clear()
        await message.answer("Вопрос уже закрыт или не найден.")
        return
    answer = (message.text or message.caption or "Ответ поддержки во вложении.").strip()
    try:
        if message.text:
            await bot.send_message(
                ticket.telegram_user_id,
                f"🛟 Ответ поддержки\n\n{answer}",
            )
        else:
            await bot.send_message(ticket.telegram_user_id, "🛟 Ответ поддержки:")
            await bot.copy_message(
                ticket.telegram_user_id,
                message.chat.id,
                message.message_id,
            )
    except Exception:
        logger.exception("Could not deliver support answer for ticket %s", ticket.id)
        await message.answer("Telegram не принял ответ. Вопрос оставлен открытым.")
        return
    await support_tickets.answer(
        ticket.id,
        text=answer[:4000],
        actor_id=message.from_user.id,
    )
    await state.clear()
    await message.answer(f"✅ Ответ по вопросу #{ticket.id} отправлен клиенту от имени бота.")

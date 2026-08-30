from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.security import TokenSigner
from app.support.faq import FAQ_BY_KEY, faq_for_text
from app.support.handlers import support_question, support_reply_send
from app.support.keyboards import support_admin_keyboard, support_menu_keyboard
from app.support.repository import SupportTicketRepository


def test_support_url_always_opens_the_customer_bot():
    settings = Settings(
        telegram_bot_username="@arenda312bot",
        support_url="https://t.me/old_admin",
    )

    assert settings.support_bot_url == "https://t.me/arenda312bot?start=support"


def test_faq_answers_only_confident_common_questions():
    assert faq_for_text("Как получить номер собственника?").key == "phone"
    assert faq_for_text("куда отправить чек после оплаты").key == "payment"
    assert faq_for_text("сколько стоит тариф на неделю").key == "week"
    assert faq_for_text("У меня необычная проблема с конкретной квартирой") is None


def test_support_menu_contains_every_faq_and_close_button():
    keyboard = support_menu_keyboard()
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

    assert set(callbacks[:-1]) == {f"support:faq:{key}" for key in FAQ_BY_KEY}
    assert callbacks[-1] == "support:close"


def test_admin_reply_button_is_signed():
    signer = TokenSigner("support-test-secret-123")
    keyboard = support_admin_keyboard(42, signer=signer)
    callback_data = keyboard.inline_keyboard[0][0].callback_data
    token = callback_data.removeprefix("support:reply:")

    assert signer.verify_id("support-reply", token) == 42


@pytest.mark.asyncio
async def test_support_ticket_lifecycle(repositories):
    _, _, sessions = repositories
    tickets = SupportTicketRepository(sessions)
    row = await tickets.create(
        telegram_user_id=777,
        username="customer",
        first_name="Клиент",
        question="Нестандартный вопрос",
    )

    assert row.status == "open"
    assert [ticket.id for ticket in await tickets.open_tickets()] == [row.id]
    await tickets.mark_notified(row.id, 12345)
    assert (await tickets.get(row.id)).admin_message_id == 12345
    assert await tickets.answer(row.id, text="Ответ", actor_id=999) is True
    answered = await tickets.get(row.id)
    assert answered.status == "answered"
    assert answered.answer == "Ответ"
    assert await tickets.answer(row.id, text="Повтор", actor_id=999) is False


@pytest.mark.asyncio
async def test_common_question_is_answered_without_admin_handoff():
    message = SimpleNamespace(
        text="Как получить номер собственника?",
        answer=AsyncMock(),
    )
    tickets = AsyncMock()

    await support_question(
        message,
        settings=Settings(admin_user_id=999),
        support_tickets=tickets,
        signer=TokenSigner("support-test-secret-123"),
        bot=AsyncMock(),
    )

    tickets.create.assert_not_awaited()
    assert "Посмотреть номер" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_unknown_question_is_handed_to_admin():
    user = SimpleNamespace(id=777, username="customer", first_name="Клиент")
    message = SimpleNamespace(
        text="У меня необычная проблема с конкретной квартирой",
        from_user=user,
        chat=SimpleNamespace(id=777),
        message_id=10,
        answer=AsyncMock(),
    )
    tickets = AsyncMock()
    tickets.create.return_value = SimpleNamespace(id=51)
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=900)

    await support_question(
        message,
        settings=Settings(admin_user_id=999),
        support_tickets=tickets,
        signer=TokenSigner("support-test-secret-123"),
        bot=bot,
    )

    tickets.create.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 999
    tickets.mark_notified.assert_awaited_once_with(51, 900)
    assert "передан администратору" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_answer_is_delivered_from_bot_and_closes_ticket():
    message = SimpleNamespace(
        text="Проверьте кнопку ещё раз — теперь всё работает.",
        caption=None,
        from_user=SimpleNamespace(id=999),
        chat=SimpleNamespace(id=999),
        message_id=77,
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"support_ticket_id": 51}
    tickets = AsyncMock()
    tickets.get.return_value = SimpleNamespace(
        id=51,
        status="open",
        telegram_user_id=777,
    )
    bot = AsyncMock()

    await support_reply_send(
        message,
        state=state,
        settings=Settings(admin_user_id=999),
        support_tickets=tickets,
        bot=bot,
    )

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 777
    tickets.answer.assert_awaited_once_with(
        51,
        text="Проверьте кнопку ещё раз — теперь всё работает.",
        actor_id=999,
    )
    state.clear.assert_awaited_once()

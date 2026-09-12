from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.security import TokenSigner
from app.support.faq import FAQ_BY_KEY, fallback_answer, faq_for_text
from app.support.handlers import support_question
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
async def test_common_question_is_answered_directly():
    message = SimpleNamespace(
        text="Как получить номер собственника?",
        answer=AsyncMock(),
    )
    await support_question(message)

    assert "Посмотреть номер" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_unknown_question_gets_self_service_answer_without_handoff():
    message = SimpleNamespace(
        text="У меня необычная проблема с конкретной квартирой",
        answer=AsyncMock(),
    )
    await support_question(message)

    answer = message.answer.await_args.args[0]
    assert answer == fallback_answer(message.text)
    assert "администратор" not in answer.casefold()


def test_typed_commands_are_redirected_to_buttons():
    answer = fallback_answer("/want")
    assert "кнопками" in answer
    assert "Подать заявку" in answer

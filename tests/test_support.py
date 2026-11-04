from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.security import TokenSigner
from app.support.faq import FAQ_BY_KEY, fallback_answer, faq_for_text
from app.support.handlers import (
    SUPPORT_BUTTONS_ONLY_TEXT,
    SUPPORT_MENU_TEXT,
    begin_operator_reply,
    send_operator_reply,
    submit_operator_question,
    support_back,
    support_faq_callback,
    support_operator,
    support_question,
)
from app.support.keyboards import (
    support_admin_keyboard,
    support_back_keyboard,
    support_menu_keyboard,
)
from app.support.repository import SupportTicketRepository


def test_support_url_always_opens_the_customer_bot():
    settings = Settings(
        telegram_bot_username="@arenda312bot",
        support_url="https://t.me/old_admin",
    )

    assert settings.support_bot_url == "https://t.me/arenda312bot?start=support"


def test_faq_answers_only_confident_common_questions():
    assert faq_for_text("как оплатить доступ").key == "payment"
    assert faq_for_text("сколько стоит тариф на неделю").key == "payment"
    assert faq_for_text("откуда квартиры").key == "about"
    assert faq_for_text("оплатил но номера нет").key == "review"
    assert faq_for_text("Как получить номер собственника?") is None
    assert faq_for_text("что значит автор") is None
    assert faq_for_text("У меня необычная проблема с конкретной квартирой") is None


def test_payment_faq_gives_only_customer_facing_instructions():
    assert "Получить номер" in FAQ_BY_KEY["payment"].answer
    assert "автоматически" in FAQ_BY_KEY["review"].answer
    assert "проверяется вручную" not in FAQ_BY_KEY["review"].answer
    assert "Finik" in FAQ_BY_KEY["review"].answer


def test_support_menu_contains_every_faq_and_operator_as_last_button():
    keyboard = support_menu_keyboard()
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

    assert set(callbacks[:-2]) == {f"support:faq:{key}" for key in FAQ_BY_KEY}
    assert callbacks[-2] == "support:close"
    assert callbacks[-1] == "support:operator"
    assert keyboard.inline_keyboard[-1][0].text == "👩‍💻 Помощь оператора"


def test_support_menu_is_short_and_explains_the_service():
    assert len(FAQ_BY_KEY) == 5
    assert "автоматический отбор" not in SUPPORT_MENU_TEXT
    assert "риелторы" not in SUPPORT_MENU_TEXT
    assert "автоматический отбор" in FAQ_BY_KEY["about"].answer
    assert "риелторы" in FAQ_BY_KEY["about"].answer
    assert "не отвечаем" in FAQ_BY_KEY["about"].answer


def test_support_answer_has_only_back_button():
    keyboard = support_back_keyboard()

    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    assert keyboard.inline_keyboard[0][0].callback_data == "support:back"


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
async def test_typed_support_message_redirects_to_buttons():
    message = SimpleNamespace(
        text="Как оплатить доступ?",
        answer=AsyncMock(),
    )
    await support_question(message)

    assert message.answer.await_args.args[0] == SUPPORT_BUTTONS_ONLY_TEXT
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data.startswith("support:faq:")


@pytest.mark.asyncio
async def test_faq_button_replaces_menu_with_answer_and_back_button():
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(
        data="support:faq:payment",
        message=message,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(set_state=AsyncMock())

    await support_faq_callback(callback, state)

    message.edit_text.assert_awaited_once()
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "support:back"


@pytest.mark.asyncio
async def test_back_button_replaces_answer_with_full_menu():
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock())

    await support_back(callback, state)

    assert "Помощь" in message.edit_text.await_args.args[0]
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert len(keyboard.inline_keyboard) == len(FAQ_BY_KEY) + 2


@pytest.mark.asyncio
async def test_unknown_question_also_redirects_to_buttons():
    message = SimpleNamespace(
        text="У меня необычная проблема с конкретной квартирой",
        answer=AsyncMock(),
    )
    await support_question(message)

    answer = message.answer.await_args.args[0]
    assert answer == SUPPORT_BUTTONS_ONLY_TEXT
    assert "писать не нужно" in answer


@pytest.mark.asyncio
async def test_operator_button_opens_message_entry():
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock())

    await support_operator(callback, state)

    assert "Напишите вопрос" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_customer_question_is_relayed_to_admin(repositories):
    _, _, sessions = repositories
    tickets = SupportTicketRepository(sessions)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=505))
    )
    message = SimpleNamespace(
        text="Помогите разобраться с доступом",
        from_user=SimpleNamespace(
            id=777, username="customer", first_name="Клиент"
        ),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(set_state=AsyncMock())

    await submit_operator_question(
        message,
        state,
        Settings(admin_user_id=999),
        TokenSigner("support-test-secret-123"),
        tickets,
        bot,
    )

    admin_call = bot.send_message.await_args
    assert admin_call.args[0] == 999
    assert "Помогите разобраться" in admin_call.args[1]
    assert admin_call.kwargs["reply_markup"].inline_keyboard[0][0].text == "✍️ Ответить клиенту"
    ticket = (await tickets.open_tickets())[0]
    assert ticket.telegram_user_id == 777
    assert ticket.admin_message_id == 505
    assert "передано оператору" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_reply_is_relayed_to_customer(repositories):
    _, _, sessions = repositories
    tickets = SupportTicketRepository(sessions)
    ticket = await tickets.create(
        telegram_user_id=777,
        username="customer",
        first_name="Клиент",
        question="Вопрос",
    )
    signer = TokenSigner("support-test-secret-123")
    callback_message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data=f"support:reply:{signer.sign_id('support-reply', ticket.id)}",
        from_user=SimpleNamespace(id=999),
        message=callback_message,
        answer=AsyncMock(),
    )
    state = SimpleNamespace(
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        get_data=AsyncMock(return_value={"support_ticket_id": ticket.id}),
        clear=AsyncMock(),
    )

    await begin_operator_reply(
        callback, state, Settings(admin_user_id=999), signer, tickets
    )
    assert "Напишите ответ" in callback_message.answer.await_args.args[0]

    bot = SimpleNamespace(send_message=AsyncMock())
    admin_message = SimpleNamespace(
        text="Ваш доступ уже активен.",
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )
    await send_operator_reply(
        admin_message,
        state,
        Settings(admin_user_id=999),
        tickets,
        bot,
    )

    assert bot.send_message.await_args.args[0] == 777
    assert "Ваш доступ уже активен" in bot.send_message.await_args.args[1]
    assert (await tickets.get(ticket.id)).status == "answered"
    state.clear.assert_awaited_once()


def test_payment_review_never_asks_customer_to_type_payment_details():
    answer = FAQ_BY_KEY["review"].answer.casefold()
    assert "напишите" not in answer
    assert "писать" in answer
    assert "не требуется" in answer


def test_typed_commands_are_redirected_to_buttons():
    answer = fallback_answer("/want")
    assert "кнопками" in answer
    assert "Подать заявку" in answer

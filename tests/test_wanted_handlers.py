from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import start_handler
from app.config import Settings
from app.security import TokenSigner
from app.wanted.admin import wanted_admin_callback
from app.wanted.handlers import wanted_command, wanted_paid


def make_ad(**overrides):
    values = {
        "id": 12,
        "telegram_user_id": 100,
        "username": "tenant",
        "first_name": "Tenant",
        "rooms": "2",
        "district": "7 мкр",
        "budget": 35_000,
        "move_in": "сразу",
        "tenants": "семейная пара",
        "notes": "с мебелью",
        "contact": "@tenant",
        "status": "pending",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_plain_start_shows_wanted_ad_button():
    message = SimpleNamespace(
        text="/start",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())
    settings = Settings(support_url="https://t.me/support")

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=settings,
        bot=object(),
        state=state,
    )

    state.clear.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "🔎 Разместить «Ищу квартиру»"
    assert markup.inline_keyboard[0][0].callback_data == "wanted:new"


@pytest.mark.asyncio
async def test_wanted_deep_link_starts_form_immediately():
    message = SimpleNamespace(
        text="/start want",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(),
        bot=object(),
        state=state,
    )

    state.clear.assert_awaited_once()
    state.set_state.assert_awaited_once()
    assert "Сколько комнат вам нужно?" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_want_command_starts_room_form():
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

    await wanted_command(message, state)

    state.clear.assert_awaited_once()
    state.set_state.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == ["Студия", "1", "2"]


@pytest.mark.asyncio
async def test_payment_check_notifies_admin_once():
    signer = TokenSigner("a-very-long-test-secret")
    ad = make_ad()
    callback = SimpleNamespace(
        data=f"wanted-paid:{signer.sign_id('wanted-paid', ad.id)}",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_reply_markup=AsyncMock()),
    )
    wanted_ads = SimpleNamespace(
        submit_payment=AsyncMock(return_value=("created", ad)),
        claim_admin_notification=AsyncMock(return_value=True),
        release_admin_notification=AsyncMock(),
        finish_admin_notification=AsyncMock(return_value=True),
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=901)))
    settings = Settings(
        admin_user_id=999,
        finik_payment_url="https://qr.finik.kg/payment",
        support_url="https://t.me/support",
    )

    await wanted_paid(callback, wanted_ads, signer, settings, bot)

    wanted_ads.claim_admin_notification.assert_awaited_once_with(ad.id)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 999
    assert "100 сом" in bot.send_message.await_args.args[1]
    wanted_ads.finish_admin_notification.assert_awaited_once_with(ad.id, 901)


@pytest.mark.asyncio
async def test_admin_approval_publishes_to_group_and_notifies_owner():
    signer = TokenSigner("a-very-long-test-secret")
    ad = make_ad()
    callback = SimpleNamespace(
        data=f"wanted-admin:a:{signer.sign_id('wanted-admin', ad.id)}",
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    wanted_ads = SimpleNamespace(
        begin_decision=AsyncMock(return_value="publishing"),
        get=AsyncMock(return_value=ad),
        release_publication=AsyncMock(),
        mark_published=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[SimpleNamespace(message_id=777), SimpleNamespace(message_id=778)]
        )
    )
    settings = Settings(
        admin_user_id=999,
        telegram_group_id=-100123,
        support_url="https://t.me/support",
    )

    await wanted_admin_callback(callback, settings, signer, wanted_ads, bot)

    first_send = bot.send_message.await_args_list[0]
    assert first_send.args[0] == -100123
    assert first_send.args[1].startswith("🔎 Ищу")
    assert "📞 Контакты: @tenant" in first_send.args[1]
    await_args = wanted_ads.mark_published.await_args
    assert await_args.args == (ad.id, 777)
    assert bot.send_message.await_args_list[1].args[0] == ad.telegram_user_id

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import start_handler, status_button_handler
from app.config import Settings
from app.security import TokenSigner
from app.wanted.admin import wanted_admin_callback
from app.support.handlers import support_button
from app.wanted.handlers import (
    my_wanted_ads_button,
    wanted_cancel_message,
    wanted_create,
    wanted_paid,
)
from app.wanted.keyboards import wanted_public_keyboard


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


def test_wanted_payment_link_must_be_configured_by_deployment():
    settings = Settings(finik_payment_url="https://qr.finik.kg/weekly-500")
    assert settings.wanted_finik_payment_url != settings.finik_payment_url
    assert settings.wanted_finik_payment_url == ""


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
    assert markup.inline_keyboard[0][0].callback_data == "matching:mine"
    assert [button.callback_data for button in markup.inline_keyboard[1]] == [
        "matching:start",
        "matching:toggle",
    ]
    assert markup.inline_keyboard[2][0].callback_data == "wanted:new"
    assert markup.inline_keyboard[3][0].callback_data == "menu:mywanted"
    assert markup.inline_keyboard[4][0].callback_data == "menu:privacy"
    assert [button.callback_data for button in markup.inline_keyboard[5]] == [
        "menu:support",
        "menu:status",
    ]


@pytest.mark.asyncio
async def test_russian_start_alias_opens_the_same_menu():
    message = SimpleNamespace(
        text="/старт",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(),
        bot=object(),
        state=state,
    )

    assert "Сервис аренды квартир" in message.answer.await_args.args[0]
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_status_button_reports_that_bot_is_running():
    callback = SimpleNamespace(answer=AsyncMock())

    await status_button_handler(callback)

    callback.answer.assert_awaited_once_with("✅ Бот работает", show_alert=True)


@pytest.mark.asyncio
async def test_menu_support_button_opens_support_inside_the_bot():
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

    await support_button(callback, state)

    callback.answer.assert_awaited_once_with()
    state.clear.assert_awaited_once()
    state.set_state.assert_awaited_once()
    assert "Помощь" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_menu_my_wanted_button_uses_customer_id():
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), answer=AsyncMock())
    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=321),
        answer=AsyncMock(),
    )
    wanted_ads = SimpleNamespace(owned=AsyncMock(return_value=[]))

    await my_wanted_ads_button(
        callback,
        wanted_ads=wanted_ads,
        settings=Settings(),
        signer=TokenSigner("a-very-long-test-secret"),
    )

    wanted_ads.owned.assert_awaited_once_with(321)
    assert "нет заявок" in message.answer.await_args.args[0].casefold()


@pytest.mark.asyncio
async def test_my_wanted_menu_shows_add_card_only_to_numeric_admin():
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    wanted_ads = SimpleNamespace(owned=AsyncMock(return_value=[]))

    await my_wanted_ads_button(
        callback,
        wanted_ads=wanted_ads,
        settings=Settings(admin_user_id=777),
        signer=TokenSigner("a-very-long-test-secret"),
    )

    markup = message.answer.await_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.callback_data == "manual:add" for button in buttons)


@pytest.mark.asyncio
async def test_my_wanted_menu_hides_add_card_from_customer():
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=321),
        answer=AsyncMock(),
    )
    wanted_ads = SimpleNamespace(owned=AsyncMock(return_value=[]))

    await my_wanted_ads_button(
        callback,
        wanted_ads=wanted_ads,
        settings=Settings(admin_user_id=777),
        signer=TokenSigner("a-very-long-test-secret"),
    )

    markup = message.answer.await_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert all(button.callback_data != "manual:add" for button in buttons)


@pytest.mark.asyncio
async def test_wanted_form_can_be_cancelled_with_command():
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=321),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await wanted_cancel_message(message, state, Settings(admin_user_id=777))

    state.clear.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert all(button.callback_data != "manual:add" for button in buttons)


@pytest.mark.asyncio
async def test_wanted_create_does_not_lose_form_when_payment_is_unavailable():
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=321, username="tenant", first_name="Tenant"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(get_data=AsyncMock(), clear=AsyncMock())
    wanted_ads = SimpleNamespace(create=AsyncMock())

    await wanted_create(
        callback,
        state,
        wanted_ads,
        TokenSigner("a-very-long-test-secret"),
        Settings(wanted_finik_payment_url=""),
    )

    wanted_ads.create.assert_not_awaited()
    state.clear.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_plain_start_sends_menu_before_state_cleanup():
    order: list[str] = []

    async def answer(*_args, **_kwargs) -> None:
        order.append("answer")

    async def clear() -> None:
        order.append("clear")

    message = SimpleNamespace(
        text="/start",
        from_user=SimpleNamespace(id=100),
        answer=answer,
    )
    state = SimpleNamespace(clear=clear)

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(),
        bot=object(),
        state=state,
    )

    assert order == ["answer", "clear"]


def test_public_wanted_card_avoids_private_user_id_button() -> None:
    without_username = wanted_public_keyboard(
        make_ad(username=None), support_url="https://t.me/support"
    )
    assert [button.text for row in without_username.inline_keyboard for button in row] == [
        "🛟 Техподдержка"
    ]

    with_username = wanted_public_keyboard(
        make_ad(username="tenant"), support_url="https://t.me/support"
    )
    assert with_username.inline_keyboard[0][0].url == "https://t.me/tenant"


@pytest.mark.asyncio
async def test_apartment_start_link_survives_cloud_signer_mismatch():
    publishing_signer = TokenSigner("publishing-worker-secret")
    bot_signer = TokenSigner("different-runtime-secret")
    payment_token = publishing_signer.sign_start_id("payment-link", 152)
    service = SimpleNamespace(
        contact_status=AsyncMock(
            return_value=SimpleNamespace(status="new", apartment=None)
        )
    )
    message = SimpleNamespace(
        text=f"/start pay_{payment_token}",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await start_handler(
        message,
        service=service,
        signer=bot_signer,
        settings=Settings(),
        bot=object(),
        state=state,
    )

    service.contact_status.assert_awaited_once_with(100, 152)
    assert message.answer.await_args.args[0] == "Доступ к контактам на 7 дней — 499 сом."
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert [row[0].text for row in markup.inline_keyboard] == [
        "Недельный тариф — 499 сом",
        "🔒 Политика конфиденциальности",
    ]


@pytest.mark.asyncio
async def test_malformed_apartment_start_link_falls_back_to_main_menu():
    message = SimpleNamespace(
        text="/start pay_bad!-token",
        from_user=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(),
        bot=object(),
        state=state,
    )

    assert "Сервис аренды квартир" in message.answer.await_args.args[0]
    assert "Ссылка недействительна" not in message.answer.await_args.args[0]


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

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.bot.admin import payment_decision_handler
from app.models import PaymentHistory
from app.payment_plans import WEEK_PLAN
from app.payments.review import notify_payment_admin, payment_generation, review_keyboard
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_admin_decision_controls_access_and_repeated_taps(repositories, service, monkeypatch, approve):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=123, apartment_id=apartment.id,
                                             username="buyer", first_name="Buyer", plan=WEEK_PLAN)
    request = await payments.mark_payment_claimed(user_id=123, apartment_id=apartment.id)
    signer = TokenSigner("review-test-secret-long-enough")
    keyboard = review_keyboard(request, signer)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=999),
                               data=keyboard.inline_keyboard[0][0 if approve else 1].callback_data,
                               answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    settings = SimpleNamespace(admin_user_id=999, support_bot_url="https://t.me/support",
                               max_photos_per_apartment=5)
    bot = SimpleNamespace(send_message=AsyncMock())
    delivery = AsyncMock()
    monkeypatch.setattr("app.bot.admin.send_private_contact", delivery)
    assert (await service.contact_status(123, apartment.id)).status == "pending"
    await payment_decision_handler(callback, settings, service, signer, bot)
    assert (await service.contact_status(123, apartment.id)).status == ("approved" if approve else "rejected")
    await payment_decision_handler(callback, settings, service, signer, bot)
    assert delivery.await_count == int(approve)
    assert bot.send_message.await_count == int(not approve)
    async with sessions() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == int(approve)
    assert all(len(button.callback_data.encode()) <= 64 for button in keyboard.inline_keyboard[0])


@pytest.mark.asyncio
async def test_non_admin_and_tampered_review_buttons_do_not_grant_access(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=123, apartment_id=apartment.id,
                                             username=None, first_name="Buyer", plan=WEEK_PLAN)
    request = await payments.mark_payment_claimed(user_id=123, apartment_id=apartment.id)
    signer = TokenSigner("review-test-secret-long-enough")
    data = review_keyboard(request, signer).inline_keyboard[0][0].callback_data
    for actor, token in [(123, data), (999, data + "x")]:
        callback = SimpleNamespace(from_user=SimpleNamespace(id=actor), data=token, answer=AsyncMock())
        await payment_decision_handler(callback, SimpleNamespace(admin_user_id=999), service, signer, object())
    assert (await service.contact_status(123, apartment.id)).status == "pending"


@pytest.mark.asyncio
async def test_notification_retries_failure_and_deduplicates(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await service.begin_payment(user_id=123, apartment_id=apartment.id,
                               username=None, first_name="<Buyer>", plan=WEEK_PLAN)
    request = await payments.mark_payment_claimed(user_id=123, apartment_id=apartment.id)
    signer = TokenSigner("review-test-secret-long-enough")
    settings = SimpleNamespace(admin_user_id=999)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("Telegram unavailable"),
                                                             SimpleNamespace(message_id=42)]))
    with pytest.raises(RuntimeError):
        await notify_payment_admin(bot, settings, payments, signer, request)
    assert (await payments.get_request(request.id)).admin_message_id is None
    await notify_payment_admin(bot, settings, payments, signer, request)
    await notify_payment_admin(bot, settings, payments, signer, request)
    assert bot.send_message.await_count == 2
    assert (await payments.get_request(request.id)).admin_message_id == 42
    assert "&lt;Buyer&gt;" in bot.send_message.await_args.args[1]
    assert (await service.contact_status(123, apartment.id)).status == "pending"


@pytest.mark.asyncio
async def test_old_admin_button_cannot_approve_resubmitted_payment(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await service.begin_payment(user_id=123, apartment_id=apartment.id,
                               username=None, first_name="Buyer", plan=WEEK_PLAN)
    request = await payments.mark_payment_claimed(user_id=123, apartment_id=apartment.id)
    old_generation = payment_generation(request)
    await service.decide(request.id, approve=False, actor_id=999)
    await service.begin_payment(user_id=123, apartment_id=apartment.id,
                               username=None, first_name="Buyer", plan=WEEK_PLAN)
    await payments.mark_payment_claimed(user_id=123, apartment_id=apartment.id)
    assert await service.decide(request.id, approve=True, actor_id=999, generation=old_generation) == "stale"
    assert (await service.contact_status(123, apartment.id)).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["w", "m"])
async def test_native_checkout_requires_checkbox_and_can_be_unchecked(plan):
    from app.bot.handlers import payment_consent_handler, payment_uncheck_handler
    from app.telegram.keyboards import payment_keyboard

    signer = TokenSigner("review-test-secret-long-enough")
    price = 999 if plan == "m" else 499
    keyboard = payment_keyboard(42, signer=signer, payment_url="https://qr.finik.kg/test",
                                support_url="https://t.me/test", price=price)
    assert keyboard.inline_keyboard[0][0].url is None
    settings = SimpleNamespace(finik_payment_url="https://qr.finik.kg/week",
                               monthly_finik_payment_url="https://qr.finik.kg/month",
                               support_bot_url="https://t.me/test")
    callback = SimpleNamespace(from_user=SimpleNamespace(id=123, username=None, first_name="Buyer"),
                               data=keyboard.inline_keyboard[1][0].callback_data,
                               answer=AsyncMock(), message=SimpleNamespace(edit_reply_markup=AsyncMock()))
    consents = SimpleNamespace(accept=AsyncMock())
    service = SimpleNamespace(contact_status=AsyncMock(return_value=SimpleNamespace(status="awaiting_receipt")),
                              begin_payment=AsyncMock())
    await payment_consent_handler(callback, signer, settings, consents, service)
    consents.accept.assert_awaited_once_with(123)
    agreed = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert agreed.inline_keyboard[0][0].url == (settings.monthly_finik_payment_url if plan == "m" else settings.finik_payment_url)
    callback.data = agreed.inline_keyboard[1][0].callback_data
    await payment_uncheck_handler(callback, signer, settings)
    locked = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert locked.inline_keyboard[0][0].url is None

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.bot.admin import payment_review_callback
from app.config import Settings
from app.models import PaymentHistory
from app.payment_plans import WEEK_PLAN
from app.payments.review import notify_payment_admin, payment_review_keyboard
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.fixture
def review_context():
    settings = Settings(admin_user_id=999, callback_secret="manual-payment-secret-long")
    signer = TokenSigner(settings.callback_secret)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=515)))
    return settings, signer, bot


async def pending_checkout(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=10101))
    checkout = await service.begin_payment(user_id=101, apartment_id=apartment.id,
        username="buyer", first_name="Buyer", plan=WEEK_PLAN)
    request = await payments.mark_payment_claimed(user_id=101, apartment_id=apartment.id)
    assert request.id == checkout.request.id
    return apartment, request


def callback_for(request_id, signer, *, approve=True, actor=999, message_id=515):
    markup = payment_review_keyboard(request_id, signer)
    data = markup.inline_keyboard[0][0 if approve else 1].callback_data
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=actor), answer=AsyncMock(),
        message=SimpleNamespace(message_id=message_id, edit_reply_markup=AsyncMock(), answer=AsyncMock()))


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_only_admin_decision_unlocks_contacts(repositories, service, review_context, monkeypatch, approve):
    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    payments = repositories[1]
    assert await notify_payment_admin(bot, payments, settings, signer, request) is True
    assert await notify_payment_admin(bot, payments, settings, signer, request) is False
    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args
    assert sent.args[0] == 999
    assert "499" in sent.args[1] and "@buyer" in sent.args[1]
    assert [b.text for b in sent.kwargs["reply_markup"].inline_keyboard[0]] == ["Дать доступ", "Отказать"]
    assert (await service.contact_status(101, apartment.id)).status == "pending"
    delivery = AsyncMock()
    monkeypatch.setattr("app.bot.admin.send_private_contact", delivery)
    callback = callback_for(request.id, signer, approve=approve)
    await payment_review_callback(callback, settings, service, signer, bot)
    result = await service.contact_status(101, apartment.id)
    assert result.status == ("approved" if approve else "rejected")
    assert bot.send_message.await_args.args[0] == 101
    if approve:
        delivery.assert_awaited_once()
        assert delivery.await_args.kwargs["user_id"] == 101
        assert result.access_expires_at is not None
    else:
        delivery.assert_not_awaited()
    # Repeated clicks cannot issue more paid grants or send another contact card.
    count = bot.send_message.await_count
    await payment_review_callback(callback, settings, service, signer, bot)
    assert bot.send_message.await_count == count
    async with repositories[2]() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == int(approve)
    stored = await payments.get_request(request.id)
    assert (stored.approved_by if approve else stored.rejected_by) == 999


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["actor", "signature", "stale", "action"])
async def test_review_buttons_cannot_bypass_admin_or_checkout_identity(repositories, service, review_context, bad):
    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    await notify_payment_admin(bot, repositories[1], settings, signer, request)
    callback = callback_for(request.id, signer)
    if bad == "actor":
        callback.from_user.id = 101
    elif bad == "signature":
        callback.data += "tampered"
    elif bad == "stale":
        callback.message.message_id = 514
    else:
        callback.data = callback.data.replace("payment:a:", "payment:r:")
    await payment_review_callback(callback, settings, service, signer, bot)
    assert (await service.contact_status(101, apartment.id)).status == "pending"
    assert bot.send_message.await_count == 1
    callback.message.edit_reply_markup.assert_not_awaited()
    async with repositories[2]() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == 0


@pytest.mark.asyncio
async def test_telegram_failure_releases_notification_for_retry(repositories, service, review_context):
    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError):
        await notify_payment_admin(bot, repositories[1], settings, signer, request)
    stored = await repositories[1].get_request(request.id)
    assert stored.admin_message_id is None
    assert (await service.contact_status(101, apartment.id)).status == "pending"
    bot.send_message.side_effect = None
    assert await notify_payment_admin(bot, repositories[1], settings, signer, stored)
    assert (await repositories[1].get_request(request.id)).admin_message_id == 515


@pytest.mark.asyncio
async def test_rejected_payment_cannot_be_revived_by_claim_or_finik(repositories, service, review_context):
    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    payments = repositories[1]
    await payments.prepare_provider_payment(request.id, "manual-test")
    await notify_payment_admin(bot, payments, settings, signer, request)
    assert await service.decide(request.id, approve=False, actor_id=999) == "rejected"
    await payments.apply_provider_result("manual-test", succeeded=True, amount=499)
    assert (await payments.mark_payment_claimed(user_id=101, apartment_id=apartment.id)).status == "rejected"
    assert (await service.contact_status(101, apartment.id)).status == "rejected"
    # The reused checkout must reject the previous admin message's buttons.
    reopened = await service.begin_payment(user_id=101, apartment_id=apartment.id, username=None, first_name="Buyer", plan=WEEK_PLAN)
    assert reopened.request.id == request.id
    new_claim = await payments.mark_payment_claimed(user_id=101, apartment_id=apartment.id)
    bot.send_message.return_value.message_id = 516
    await notify_payment_admin(bot, payments, settings, signer, new_claim)
    assert await service.decide(request.id, approve=True, actor_id=999, expected_admin_message_id=515) == "stale"
    assert await payments.decide(request.id, approve=True, admin_id=0) == "forbidden"
    assert (await service.contact_status(101, apartment.id)).status == "pending"


@pytest.mark.asyncio
async def test_failed_customer_delivery_does_not_undo_admin_grant(repositories, service, review_context, monkeypatch):
    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    await notify_payment_admin(bot, repositories[1], settings, signer, request)
    monkeypatch.setattr("app.bot.admin.send_private_contact", AsyncMock(side_effect=RuntimeError("delivery failed")))
    await payment_review_callback(callback_for(request.id, signer), settings, service, signer, bot)
    assert (await service.contact_status(101, apartment.id)).status == "approved"


@pytest.mark.asyncio
async def test_miniapp_claim_retries_notification_failure_without_exposing_phone(repositories, service, monkeypatch, review_context):
    import httpx
    from app import web
    from app.config import get_settings
    from tests.test_web import miniapp_init_data

    settings, signer, bot = review_context
    apartment, request = await pending_checkout(repositories, service)
    for key, value in {"RUN_BOT": "true", "TELEGRAM_BOT_TOKEN": "123456:test-token",
                       "ADMIN_USER_ID": "999", "CALLBACK_SECRET": settings.callback_secret}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr(web, "_bot_runtime", SimpleNamespace(bot=bot, workflow_data={
        "service": service, "payments": repositories[1]}))
    payload = {"init_data": miniapp_init_data(bot_token="123456:test-token", user_id=101),
               "start_param": signer.sign_start_id("miniapp-apartment", apartment.id)}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
            bot.send_message.side_effect = RuntimeError("Telegram unavailable")
            failed = await client.post("/miniapp/api/access", json=payload)
            assert failed.status_code == 503
            assert "phone" not in failed.json()
            assert (await service.contact_status(101, apartment.id)).status == "pending"
            bot.send_message.side_effect = None
            retried = await client.post("/miniapp/api/access", json=payload)
            assert retried.status_code == 200
            assert retried.json()["status"] == "pending"
            assert "phone" not in retried.json()
            again = await client.post("/miniapp/api/access", json=payload)
            assert again.json()["status"] == "pending"
            assert bot.send_message.await_count == 2  # First failed, retry delivered once.
            assert await service.decide(request.id, approve=True, actor_id=999) == "approved"
            granted = await client.post("/miniapp/api/session", json=payload)
            assert granted.json()["status"] == "approved"
            assert granted.json()["phone"].replace(" ", "") == apartment.phone
    finally:
        get_settings.cache_clear()

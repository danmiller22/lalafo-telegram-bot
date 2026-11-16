from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import LIFETIME_PLAN, LIFETIME_PRICE, WEEK_PLAN
from tests.helpers import make_ad


@pytest.mark.asyncio
@pytest.mark.parametrize("via_webhook", [False, True])
async def test_lifetime_grant_covers_other_and_future_apartments(repositories, service, monkeypatch, via_webhook):
    apartments, payments, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=88001))
    submission = await service.begin_payment(user_id=880, apartment_id=first.id, username="lifetime", first_name="Test", plan=LIFETIME_PLAN)
    if via_webhook:
        await payments.prepare_provider_payment(submission.request.id, "lifetime-test")
        outcome, _ = await payments.apply_provider_result("lifetime-test", succeeded=True, amount=499)
        assert outcome == "amount_mismatch"
        assert (await service.contact_status(880, first.id)).status != "approved"
        outcome, _ = await payments.apply_provider_result("lifetime-test", succeeded=True, amount=LIFETIME_PRICE)
        assert outcome == "approved"
        assert (await service.contact_status(880, first.id)).status == "approved"
        outcome, _ = await payments.apply_provider_result("lifetime-test", succeeded=True, amount=LIFETIME_PRICE)
        assert outcome == "already_approved"
    else:
        await payments.mark_payment_claimed(user_id=880, apartment_id=first.id)
        await payments.mark_payment_claimed(user_id=880, apartment_id=first.id)
    expected_decision = "already_approved" if via_webhook else "approved"
    assert await service.decide(
        submission.request.id, approve=True, actor_id=999
    ) == expected_decision
    async with sessions() as session:
        history = (await session.scalars(select(PaymentHistory))).all()
    assert len(history) == 1
    assert history[0].amount == 999
    assert history[0].plan == LIFETIME_PLAN
    assert history[0].access_expires_at is None
    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2200, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.payments.repository.datetime", FutureClock)
    second = await apartments.upsert_discovered(make_ad(lalafo_id=88002))
    access = await service.contact_status(880, second.id)
    assert access.status == "approved"
    assert access.plan == LIFETIME_PLAN
    assert access.access_expires_at is None
    assert (await service.contact_status(881, second.id)).status == "unpaid"
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == submission.request.id).values(status="rejected"))
    assert (await service.contact_status(880, second.id)).status == "unpaid"


@pytest.mark.asyncio
async def test_old_pending_checkout_can_switch_to_lifetime(repositories, service):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=88003))
    old = await service.begin_payment(user_id=880, apartment_id=apartment.id, username=None, first_name="Test", plan=WEEK_PLAN)
    await payments.prepare_provider_payment(old.request.id, "old-weekly")
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == old.request.id).values(status="pending"))
    new = await service.begin_payment(user_id=880, apartment_id=apartment.id, username=None, first_name="Test", plan=LIFETIME_PLAN)
    assert new.request.plan == LIFETIME_PLAN
    assert new.request.status == "awaiting_receipt"
    assert new.request.provider_payment_id is None
    assert (await payments.apply_provider_result("old-weekly", succeeded=True, amount=399))[0] == "missing"


@pytest.mark.asyncio
async def test_weekly_miniapp_checkout_and_automatic_access_end_to_end(repositories, service, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import httpx
    from sqlalchemy import select
    from app import web
    from app.config import get_settings
    from app.models import XPayAttempt
    from app.xpay import XPayClient, reconcile
    from app.security import TokenSigner
    from app.terms import TermsConsentRepository
    from tests.test_web import miniapp_init_data
    apartments, payments, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=88101))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=88102))
    for key, value in {"ADMIN_USER_ID":"999", "RUN_BOT":"true", "TELEGRAM_BOT_TOKEN":"123456:test-token",
        "CALLBACK_SECRET":"c"*32, "XPAY_CLIENT_ID":"id", "XPAY_CLIENT_SECRET":"secret",
        "XPAY_SERVICE_UUID":"uuid", "TELEGRAM_WEBHOOK_URL":"https://example.test/telegram/webhook"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    consents = TermsConsentRepository(sessions)
    monkeypatch.setattr(web, "_bot_runtime", SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),
        workflow_data={"service":service, "payments":payments, "terms_consents":consents}))
    create = AsyncMock(return_value=("Provider123", "https://pay.xpay.kg/checkout"))
    monkeypatch.setattr(XPayClient, "create", create)
    monkeypatch.setattr(XPayClient, "status", AsyncMock(return_value={"qr_transaction_id":"Provider123",
        "pay_status":"COMPLETED", "amount":49900, "request_amount":49900, "transaction_uuid":"uuid"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
        payload = {"init_data":miniapp_init_data(bot_token="123456:test-token", user_id=880),
            "start_param":TokenSigner("c"*32).sign_start_id("miniapp-apartment",first.id), "plan":"week"}
        assert (await client.post("/miniapp/api/session",json=payload)).json()["status"] == "unpaid"
        prepared = await client.post("/miniapp/api/prepare",json=payload)
        assert prepared.status_code == 200 and prepared.json()["payment_url"] == "https://pay.xpay.kg/checkout"
        assert (await client.post("/miniapp/api/access",json=payload)).status_code == 409
        started = await client.post("/miniapp/api/start",json=payload)
        assert started.status_code == 200 and started.json()["payment_url"] == prepared.json()["payment_url"]
        assert create.await_count == 1 and await consents.accepted(880)
        async with sessions() as session:
            token = (await session.scalar(select(XPayAttempt))).token
        assert (await reconcile(sessions, get_settings(), token))[0] == "approved"
        assert (await client.post("/miniapp/api/access",json=payload)).json()["status"] == "approved"
        payload["start_param"] = TokenSigner("c"*32).sign_start_id("miniapp-apartment",second.id)
        assert (await client.post("/miniapp/api/session",json=payload)).json()["status"] == "approved"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_expired_lifetime_finik_link_is_replaced(repositories, service):
    from datetime import timedelta
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=88201))
    submission = await service.begin_payment(user_id=882, apartment_id=apartment.id, username=None, first_name="Test", plan=LIFETIME_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "first-link")
    await payments.set_provider_payment_url(submission.request.id, "https://qr.finik.kg/first")
    fresh = await payments.prepare_provider_payment(submission.request.id, "unused-link")
    assert fresh.provider_payment_id == "first-link"
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == submission.request.id).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    renewed = await payments.prepare_provider_payment(submission.request.id, "renewed-link")
    assert renewed.provider_payment_id == "renewed-link"
    assert renewed.provider_payment_url is None


@pytest.mark.asyncio
async def test_lifetime_access_survives_original_checkout_cleanup(repositories, service):
    from sqlalchemy import delete
    apartments, payments, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=88301))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=88302))
    submission = await service.begin_payment(user_id=883, apartment_id=first.id, username=None, first_name="Test", plan=LIFETIME_PLAN)
    await payments.mark_payment_claimed(user_id=883, apartment_id=first.id)
    assert await service.decide(submission.request.id, approve=True, actor_id=999) == "approved"
    # Match the checkout deletion caused by the apartment FK cascade.
    async with sessions.begin() as session:
        await session.execute(delete(PaymentRequest).where(PaymentRequest.id == submission.request.id))
    access = await service.contact_status(883, second.id)
    assert access.status == "approved"
    assert access.plan == LIFETIME_PLAN
    assert access.access_expires_at is None
    assert (await service.contact_status(884, second.id)).status == "unpaid"

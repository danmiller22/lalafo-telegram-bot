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
        assert outcome == "awaiting_confirmation"
        assert (await service.contact_status(880, first.id)).status == "awaiting_receipt"
        await payments.mark_payment_claimed(user_id=880, apartment_id=first.id)
        outcome, _ = await payments.apply_provider_result("lifetime-test", succeeded=True, amount=LIFETIME_PRICE)
        assert outcome == "pending"
    else:
        await payments.mark_payment_claimed(user_id=880, apartment_id=first.id)
        await payments.mark_payment_claimed(user_id=880, apartment_id=first.id)
    assert await service.decide(submission.request.id, approve=True, actor_id=999) == "approved"
    async with sessions() as session:
        history = (await session.scalars(select(PaymentHistory))).all()
    assert len(history) == 1
    assert history[0].amount == 699
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
    assert (await payments.apply_provider_result("old-weekly", succeeded=True, amount=499))[0] == "missing"


@pytest.mark.asyncio
async def test_weekly_miniapp_checkout_and_delayed_access_end_to_end(repositories, service, monkeypatch):
    import json
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import httpx
    from app import web
    from app.config import get_settings
    from app.finik import FinikClient
    from app.security import TokenSigner
    from app.terms import TermsConsentRepository
    from tests.test_finik import _keys
    from tests.test_web import miniapp_init_data

    apartments, payments, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=88101))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=88102))
    private_pem, _ = _keys()
    for key, value in {
        "ADMIN_USER_ID": "999", "RUN_BOT": "true", "TELEGRAM_BOT_TOKEN": "123456:test-token",
        "CALLBACK_SECRET": "c" * 32, "FINIK_API_KEY": "test",
        "FINIK_ACCOUNT_ID": "test-merchant", "FINIK_PRIVATE_KEY_PEM": private_pem,
        "FINIK_PRIVATE_KEY_B64": "", "WEEKLY_FINIK_PAYMENT_URL": "",
        "MONTHLY_FINIK_PAYMENT_URL": "https://qr.finik.kg/old-999", "LIFETIME_FINIK_PAYMENT_URL": "",
        "TELEGRAM_WEBHOOK_URL": "https://example.test/telegram/webhook",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    consents = TermsConsentRepository(sessions)
    monkeypatch.setattr(web, "_bot_runtime", SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=515))), workflow_data={"service": service, "payments": payments, "terms_consents": consents}))
    captured = []
    async def provider(request):
        captured.append(json.loads(request.content))
        assert request.headers["signature"]
        return httpx.Response(201, json={"url": "https://qr.finik.kg/weekly-500"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as finik_http:
        monkeypatch.setattr(web, "FinikClient", lambda **kwargs: FinikClient(client=finik_http, **kwargs))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
            payload = {"init_data": miniapp_init_data(bot_token="123456:test-token", user_id=880), "start_param": TokenSigner("c" * 32).sign_start_id("miniapp-apartment", first.id), "plan": "month"}
            initial = await client.post("/miniapp/api/session", json=payload)
            assert initial.json()["status"] == "unpaid"
            assert initial.json()["price"] == 499
            prepared = await client.post("/miniapp/api/prepare", json=payload)
            assert prepared.status_code == 200
            assert prepared.json()["payment_url"] == "https://qr.finik.kg/weekly-500"
            assert len(captured) == 1
            assert not await consents.accepted(880)
            assert (await service.contact_status(880, first.id)).status == "unpaid"
            premature = await client.post("/miniapp/api/access", json=payload)
            assert premature.status_code == 409
            started = await client.post("/miniapp/api/start", json=payload)
            assert started.status_code == 200
            assert started.json()["plan"] == WEEK_PLAN
            assert started.json()["payment_url"] == "https://qr.finik.kg/weekly-500"
            assert await consents.accepted(880)
            assert len(captured) == 1
            assert captured[0]["Amount"] == 499
            assert captured[0]["Data"]["description"] == "Недельный тариф"
            again = await client.post("/miniapp/api/start", json=payload)
            assert again.json()["payment_url"] == started.json()["payment_url"]
            assert len(captured) == 1
            granted = await client.post("/miniapp/api/access", json=payload)
            assert granted.json()["status"] == "pending"
            assert "phone" not in granted.json()
            pending_again = await client.post("/miniapp/api/access", json=payload)
            assert pending_again.json()["status"] == "pending"
            web._bot_runtime.bot.send_message.assert_not_awaited()
            checkout = await payments.get_access(880, first.id)
            claimed = checkout.payment_claimed_at.replace(tzinfo=timezone.utc)
            assert not await payments.approve_claims_due(now=claimed + timedelta(seconds=59))
            assert len(await payments.approve_claims_due(now=claimed + timedelta(seconds=60))) == 1
            repeated = await client.post("/miniapp/api/access", json=payload)
            assert repeated.json()["status"] == "approved"
            already_ready = await client.post("/miniapp/api/prepare", json=payload)
            assert already_ready.json()["status"] == "approved"
            assert len(captured) == 1
            payload["start_param"] = TokenSigner("c" * 32).sign_start_id("miniapp-apartment", second.id)
            next_card = await client.post("/miniapp/api/session", json=payload)
            assert next_card.json()["status"] == "approved"
            assert next_card.json()["phone"]
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

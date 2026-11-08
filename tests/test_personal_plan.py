from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from app.matching.repository import MatchingRepository
from app.models import Apartment, ApartmentMatchDelivery, ApartmentSearchProfile, PaymentHistory, PersonalMatchReservation
from app.payment_plans import PERSONAL_PLAN, PERSONAL_PRICE, PERSONAL_LIMIT, expires_at_for, plan_price
from tests.helpers import make_ad


async def purchase(apartments, payments, user_id=123, plan=PERSONAL_PLAN):
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=909001))
    result = await payments.submit(user_id=user_id, apartment_id=apartment.id,
                                   username=None, first_name="Test", plan=plan)
    await payments.mark_payment_claimed(user_id=user_id, apartment_id=apartment.id)
    assert await payments.decide(result.request.id, approve=True, admin_id=999) == "approved"
    return apartment


@pytest.mark.asyncio
async def test_personal_purchase_price_duration_and_persistent_quota(repositories):
    apartments, payments, sessions = repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=123, rooms=["1"], districts=["Центр"],
                                all_districts=False, max_budget=40000, source="personal")
    await purchase(apartments, payments)
    grant, used = await matching.personal_status(123)
    assert used == 0
    assert grant.amount == PERSONAL_PRICE == plan_price(PERSONAL_PLAN) == 1499
    assert grant.access_expires_at == expires_at_for(PERSONAL_PLAN, grant.paid_at)
    assert grant.access_expires_at - grant.paid_at == timedelta(days=30)
    for i in range(24):
        apartment = await apartments.upsert_discovered(make_ad(lalafo_id=910000 + i, district="Центр"))
        await apartments.mark_published(apartment.id, chat_id=-100, message_id=i + 1)
    # Future-expensive/wrong-area listings never consume the paid allowance.
    wrong = await apartments.upsert_discovered(make_ad(lalafo_id=919999, district="Асанбай"))
    await apartments.mark_published(wrong.id, chat_id=-100, message_id=99)
    assert await matching.seed_notifications() == PERSONAL_LIMIT
    assert await matching.seed_notifications() == 0
    assert (await matching.personal_status(123))[1] == 20
    claims = []
    for _ in range(20):
        claim = await matching.claim_delivery()
        assert claim is not None and claim.apartment.district == "Центр"
        claims.append(claim)
        await matching.finish_delivery(claim.delivery_id, message_id=1)
    assert await matching.claim_delivery() is None
    await matching.save_profile(user_id=123, rooms=["1"], districts=[], all_districts=True,
                                max_budget=60000, source="personal")
    fresh = await apartments.upsert_discovered(make_ad(lalafo_id=920000))
    await apartments.mark_published(fresh.id, chat_id=-100, message_id=100)
    # Cleanup cannot refund slots; history outlives the source apartment.
    async with sessions.begin() as session:
        await session.execute(delete(ApartmentMatchDelivery))
    assert (await matching.personal_status(123))[1] == 20
    assert await matching.seed_notifications() == 0


@pytest.mark.asyncio
async def test_personal_expiry_blocks_already_queued_delivery(repositories):
    apartments, payments, sessions = repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=123, rooms=["1"], districts=[], all_districts=True,
                                max_budget=40000, source="personal")
    await purchase(apartments, payments)
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=930000))
    await apartments.mark_published(apartment.id, chat_id=-100, message_id=1)
    assert await matching.seed_notifications() == 1
    async with sessions.begin() as session:
        await session.execute(update(PaymentHistory).values(
            access_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert await matching.claim_delivery() is None
    assert await matching.seed_notifications() == 0


@pytest.mark.asyncio
async def test_personal_ambiguous_retry_uses_same_reserved_slot(repositories):
    apartments, payments, sessions = repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=123, rooms=["1"], districts=[], all_districts=True,
                                max_budget=40000, source="personal")
    await purchase(apartments, payments)
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=940000))
    await apartments.mark_published(apartment.id, chat_id=-100, message_id=1)
    assert await matching.seed_notifications() == 1
    claim = await matching.claim_delivery()
    await matching.finish_delivery(claim.delivery_id, error="TimeoutError")
    assert (await matching.personal_status(123))[1] == 1
    assert await matching.seed_notifications() == 0


@pytest.mark.asyncio
async def test_lifetime_customer_can_buy_personal_without_losing_lifetime(repositories):
    apartments, payments, sessions = repositories
    apartment = await purchase(apartments, payments, plan="lifetime")
    result = await payments.submit(user_id=123, apartment_id=apartment.id,
                                   username=None, first_name="Test", plan=PERSONAL_PLAN)
    assert result.request.plan == PERSONAL_PLAN
    assert result.outcome != "approved"
    assert (await payments.active_weekly_access(123)).plan == "lifetime"


@pytest.mark.asyncio
async def test_finik_personal_requires_1499_and_callback_is_idempotent(repositories):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=950000))
    request = (await payments.submit(user_id=123, apartment_id=apartment.id,
        username=None, first_name=None, plan=PERSONAL_PLAN)).request
    await payments.prepare_provider_payment(request.id, "personal-payment")
    assert (await payments.apply_provider_result("personal-payment", succeeded=True, amount=999))[0] == "amount_mismatch"
    assert await payments.active_personal_access(123) is None
    assert (await payments.apply_provider_result("personal-payment", succeeded=True, amount=1499))[0] == "approved"
    grant = await payments.active_personal_access(123)
    assert grant is not None and grant.amount == 1499
    assert (await payments.apply_provider_result("personal-payment", succeeded=True, amount=1499))[0] == "already_approved"
    assert (await payments.active_personal_access(123)).id == grant.id


@pytest.mark.asyncio
async def test_lifetime_customer_retains_access_without_new_checkout(repositories, service, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import BackgroundTasks
    from app import web
    from app.config import Settings
    apartments, payments, sessions = repositories
    apartment = await purchase(apartments, payments, plan="lifetime")
    runtime = SimpleNamespace(workflow_data={"service": service, "payments": payments})
    user = SimpleNamespace(id=123, username=None, first_name="Test")
    monkeypatch.setattr(web, "_miniapp_context", lambda payload: (Settings(), runtime, user, apartment.id))
    checkout = AsyncMock()
    monkeypatch.setattr(web, "_finik_checkout_url", checkout)
    payload = web.MiniAppRequest(init_data="test", start_param="test", plan="week")
    started = await web.miniapp_start_payment(payload, BackgroundTasks())
    assert started["status"] == "approved" and started["plan"] == "lifetime"
    checkout.assert_not_awaited()
    polled = await web.miniapp_session(payload, BackgroundTasks())
    assert polled["status"] == "approved" and polled["lifetime_available"] is False
    assert (await payments.active_weekly_access(123)).access_expires_at is None

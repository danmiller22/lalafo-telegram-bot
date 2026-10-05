from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.bot.handlers import paid_handler
from app.config import Settings
from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import WEEK_PLAN
from app.payments.repository import PaymentRepository
from app.payments.worker import process_payment_claims
from app.security import TokenSigner
from tests.helpers import make_ad


async def checkout(repositories, service):
    apartment = await repositories[0].upsert_discovered(make_ad(lalafo_id=12121))
    submission = await service.begin_payment(user_id=121, apartment_id=apartment.id,
        username="buyer", first_name="Buyer", plan=WEEK_PLAN)
    return apartment, submission.request


@pytest.mark.asyncio
async def test_claim_opens_access_at_sixty_seconds_and_never_earlier(repositories, service):
    apartment, request = await checkout(repositories, service)
    payments = repositories[1]
    first = await payments.mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    again = await payments.mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    assert again.payment_claimed_at == first.payment_claimed_at
    assert first.provider_payment_id is None
    assert first.receipt_file_id is None
    claimed = first.payment_claimed_at.replace(tzinfo=timezone.utc)
    assert not await payments.approve_claims_due(now=claimed + timedelta(seconds=59, milliseconds=999))
    assert (await service.contact_status(121, apartment.id)).status == "pending"
    assert len(await payments.approve_claims_due(now=claimed + timedelta(seconds=60))) == 1
    assert (await service.contact_status(121, apartment.id)).status == "approved"
    other = await repositories[0].upsert_discovered(make_ad(lalafo_id=12122))
    assert (await service.contact_status(121, other.id)).status == "approved"
    assert (await service.contact_status(122, other.id)).status == "unpaid"
    assert not await payments.approve_claims_due(now=claimed + timedelta(hours=1))
    async with repositories[2]() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == 1
        history = await session.scalar(select(PaymentHistory))
        assert history.amount == 499
        assert history.provider_payment_id.startswith("claim-")
        expiry = history.access_expires_at.replace(tzinfo=timezone.utc)
        assert expiry == claimed + timedelta(seconds=60, days=7)


@pytest.mark.asyncio
async def test_only_customer_click_starts_hold_webhook_does_not(repositories, service):
    apartment, request = await checkout(repositories, service)
    payments = repositories[1]
    await payments.prepare_provider_payment(request.id, "signed-provider")
    await payments.apply_provider_result("signed-provider", succeeded=True, amount=499)
    stored = await payments.get_request(request.id)
    assert stored.payment_claimed_at is None
    assert not await payments.approve_claims_due(now=stored.created_at.replace(tzinfo=timezone.utc) + timedelta(days=1))
    assert (await service.contact_status(121, apartment.id)).status == "awaiting_receipt"


@pytest.mark.asyncio
async def test_restart_resumes_existing_hold_and_rejection_cancels_it(repositories, service):
    apartment, request = await checkout(repositories, service)
    first = await repositories[1].mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    due = first.payment_claimed_at.replace(tzinfo=timezone.utc) + timedelta(minutes=2)
    # A new repository has no in-memory timer, yet resumes the stored claim.
    restarted = PaymentRepository(repositories[2])
    assert await service.decide(request.id, approve=False, actor_id=999) == "rejected"
    assert not await restarted.approve_claims_due(now=due)
    reopened = await service.begin_payment(user_id=121, apartment_id=apartment.id,
        username=None, first_name="Buyer", plan=WEEK_PLAN)
    assert reopened.request.payment_claimed_at is None
    second = await restarted.mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    assert len(await restarted.approve_claims_due(now=second.payment_claimed_at.replace(tzinfo=timezone.utc) + timedelta(seconds=60))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_worker_delivers_once_and_grant_survives_delivery_error(repositories, service, monkeypatch, delivery_fails):
    apartment, request = await checkout(repositories, service)
    first = await repositories[1].mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    bot = SimpleNamespace(send_message=AsyncMock())
    delivery = AsyncMock(side_effect=RuntimeError("blocked") if delivery_fails else None)
    monkeypatch.setattr("app.payments.worker.send_private_contact", delivery)
    runtime = SimpleNamespace(bot=bot, workflow_data={"payments": PaymentRepository(repositories[2]), "settings": Settings()})
    due = first.payment_claimed_at.replace(tzinfo=timezone.utc) + timedelta(seconds=60)
    assert await process_payment_claims(runtime, now=due) == 1
    assert await process_payment_claims(runtime, now=due + timedelta(seconds=10)) == 0
    assert (await service.contact_status(121, apartment.id)).status == "approved"
    bot.send_message.assert_awaited_once_with(121, "Доступ к контактам открыт.")
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_paid_button_queues_same_hold_without_contact_or_admin_message(repositories, service, monkeypatch):
    apartment, request = await checkout(repositories, service)
    signer = TokenSigner("claim-delay-secret-long")
    callback = SimpleNamespace(data="paid:" + signer.sign_id("paid", apartment.id),
        from_user=SimpleNamespace(id=121), message=None, answer=AsyncMock())
    # Use the actual legacy prefix defined by the bot.
    from app.bot.callbacks import PAID_PREFIX
    callback.data = PAID_PREFIX + signer.sign_id("paid", apartment.id)
    delivery = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.send_private_contact", delivery)
    bot = SimpleNamespace(send_message=AsyncMock())
    await paid_handler(callback, service, repositories[1], signer, Settings(), bot)
    stored = await repositories[1].get_request(request.id)
    assert stored.status == "pending" and stored.payment_claimed_at is not None
    delivery.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    callback.answer.assert_awaited_once_with("Запрос принят.", show_alert=True)


@pytest.mark.asyncio
async def test_slow_telegram_delivery_does_not_block_next_customers_access(repositories, service):
    import asyncio
    apartment, _ = await checkout(repositories, service)
    payments = repositories[1]
    first = await payments.mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    blocked = asyncio.Event()

    async def send_message(*args, **kwargs):
        await blocked.wait()

    runtime = SimpleNamespace(bot=SimpleNamespace(send_message=send_message),
        workflow_data={"payments": payments, "settings": Settings()})
    tasks = set()
    due = first.payment_claimed_at.replace(tzinfo=timezone.utc) + timedelta(seconds=61)
    try:
        assert await process_payment_claims(runtime, now=due, delivery_tasks=tasks) == 1
        await service.begin_payment(user_id=122, apartment_id=apartment.id,
            username=None, first_name="Second", plan=WEEK_PLAN)
        await payments.mark_payment_claimed(user_id=122, apartment_id=apartment.id)
        assert await process_payment_claims(runtime, now=due, delivery_tasks=tasks) == 1
        assert (await service.contact_status(121, apartment.id)).status == "approved"
        assert (await service.contact_status(122, apartment.id)).status == "approved"
        assert not blocked.is_set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

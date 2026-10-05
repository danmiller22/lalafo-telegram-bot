"""Background retries may notify administrators, but must never issue access."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.bot.handlers import paid_handler
from app.bot.callbacks import PAID_PREFIX
from app.config import Settings
from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import WEEK_PLAN
from app.payments.worker import process_payment_reviews
from app.security import TokenSigner
from tests.helpers import make_ad


async def review_setup(repositories, service):
    apartment = await repositories[0].upsert_discovered(make_ad(lalafo_id=12121))
    submission = await service.begin_payment(user_id=121, apartment_id=apartment.id,
        username="buyer", first_name="Buyer", plan=WEEK_PLAN)
    request = await repositories[1].mark_payment_claimed(user_id=121, apartment_id=apartment.id)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=515)))
    settings = Settings(admin_user_id=999)
    signer = TokenSigner("manual-review-test-secret-long")
    runtime = SimpleNamespace(bot=bot, workflow_data={"payments": repositories[1],
        "settings": settings, "signer": signer})
    return apartment, request, runtime


@pytest.mark.asyncio
async def test_old_overdue_hold_and_provider_success_never_grant_access(repositories, service):
    apartment, request, runtime = await review_setup(repositories, service)
    payments = repositories[1]
    await payments.prepare_provider_payment(request.id, "provider-confirmed")
    await payments.apply_provider_result("provider-confirmed", succeeded=True, amount=499)
    async with repositories[2].begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == request.id)
            .values(payment_claimed_at=datetime.now(timezone.utc) - timedelta(days=1)))
    assert await process_payment_reviews(runtime) == 1
    assert await process_payment_reviews(runtime) == 0
    assert (await service.contact_status(121, apartment.id)).status == "pending"
    async with repositories[2]() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == 0
    assert await service.decide(request.id, approve=True, actor_id=999) == "approved"
    assert (await service.contact_status(121, apartment.id)).status == "approved"


@pytest.mark.asyncio
async def test_review_worker_recovers_failed_notification_without_duplicates(repositories, service):
    apartment, request, runtime = await review_setup(repositories, service)
    runtime.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    assert await process_payment_reviews(runtime) == 0
    assert (await repositories[1].get_request(request.id)).admin_message_id is None
    runtime.bot.send_message.side_effect = None
    assert await process_payment_reviews(runtime) == 1
    assert await process_payment_reviews(runtime) == 0
    assert runtime.bot.send_message.await_count == 2
    assert runtime.bot.send_message.await_args.args[0] == 999
    assert (await service.contact_status(121, apartment.id)).status == "pending"


@pytest.mark.asyncio
async def test_legacy_paid_button_notifies_admin_without_issuing_contacts(repositories, service, monkeypatch):
    apartment, request, runtime = await review_setup(repositories, service)
    signer = runtime.workflow_data["signer"]
    callback = SimpleNamespace(data=PAID_PREFIX + signer.sign_id("paid", apartment.id),
        from_user=SimpleNamespace(id=121), message=None, answer=AsyncMock())
    delivery = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.send_private_contact", delivery)
    await paid_handler(callback, service, repositories[1], signer,
        runtime.workflow_data["settings"], runtime.bot)
    stored = await repositories[1].get_request(request.id)
    assert stored.status == "pending"
    assert stored.payment_claimed_at is None
    assert stored.admin_message_id == 515
    delivery.assert_not_awaited()
    runtime.bot.send_message.assert_awaited_once()
    callback.answer.assert_awaited_once_with("Оплата отправлена на проверку. Ожидайте подтверждения.", show_alert=True)


@pytest.mark.asyncio
async def test_notifications_scan_skips_already_notified_pending_rows(repositories, service):
    apartment, request, runtime = await review_setup(repositories, service)
    await repositories[1].set_admin_message(request.id, 123)
    for user_id in range(200, 255):
        await service.begin_payment(user_id=user_id, apartment_id=apartment.id,
            username=None, first_name="User", plan=WEEK_PLAN)
        await repositories[1].mark_payment_claimed(user_id=user_id, apartment_id=apartment.id)
    assert await process_payment_reviews(runtime) == 50
    assert await process_payment_reviews(runtime) == 5
    assert await process_payment_reviews(runtime) == 0
    assert runtime.bot.send_message.await_count == 55
    assert (await service.contact_status(121, apartment.id)).status == "pending"

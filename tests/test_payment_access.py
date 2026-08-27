from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.payments.service import PaymentService
from app.models import PaymentRequest
from app.payment_plans import WEEK_PLAN
from app.lalafo.models import PHONE_SOURCE_VERSION
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_payment_state_machine(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())

    denied = await service.contact_status(100, apartment.id)
    assert denied.status == "unpaid"

    first = await service.begin_payment(
        user_id=100,
        apartment_id=apartment.id,
        username="buyer",
        first_name="Buyer",
        plan=WEEK_PLAN,
    )
    assert first.outcome == "created"
    duplicate = await service.begin_payment(
        user_id=100,
        apartment_id=apartment.id,
        username="buyer",
        first_name="Buyer",
        plan=WEEK_PLAN,
    )
    assert duplicate.outcome == "awaiting_receipt"
    assert (await service.contact_status(100, apartment.id)).status == "awaiting_receipt"
    receipt = await service.submit_receipt(
        user_id=100, file_id="receipt-photo", file_type="photo"
    )
    assert receipt is not None
    assert (await service.contact_status(100, apartment.id)).status == "pending"

    assert await service.decide(first.request.id, approve=True, actor_id=111) == "forbidden"
    assert await service.decide(first.request.id, approve=True, actor_id=999) == "approved"
    assert await service.decide(first.request.id, approve=False, actor_id=999) == "already_approved"
    approved = await service.contact_status(100, apartment.id)
    assert approved.status == "approved"
    assert approved.apartment.phone == "+996555123456"
    assert approved.access_expires_at is not None


@pytest.mark.asyncio
async def test_rejection_allows_resubmission(repositories, service):
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=222))
    submission = await service.begin_payment(
        user_id=200,
        apartment_id=apartment.id,
        username=None,
        first_name="No username",
        plan=WEEK_PLAN,
    )
    await service.submit_receipt(user_id=200, file_id="receipt", file_type="document")
    assert await service.decide(submission.request.id, approve=False, actor_id=999) == "rejected"
    assert (await service.contact_status(200, apartment.id)).status == "rejected"
    retried = await service.begin_payment(
        user_id=200,
        apartment_id=apartment.id,
        username=None,
        first_name="No username",
        plan=WEEK_PLAN,
    )
    assert retried.outcome == "created"
    assert retried.request.id == submission.request.id


@pytest.mark.asyncio
async def test_missing_or_inactive_apartment_denies_access(repositories, service):
    apartments, _, _ = repositories
    assert (await service.contact_status(1, 999999)).status == "unavailable"
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=333))
    await apartments.mark_inactive(apartment.id)
    with pytest.raises(LookupError):
        await service.begin_payment(
            user_id=1,
            apartment_id=apartment.id,
            username=None,
            first_name="User",
            plan=WEEK_PLAN,
        )


@pytest.mark.asyncio
async def test_admin_notification_claim_is_atomic_and_retryable(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=444))
    submission = await service.begin_payment(
        user_id=300,
        apartment_id=apartment.id,
        username="buyer",
        first_name="Buyer",
        plan=WEEK_PLAN,
    )
    await service.submit_receipt(user_id=300, file_id="receipt", file_type="photo")

    assert await payments.claim_admin_notification(submission.request.id) is True
    assert await payments.claim_admin_notification(submission.request.id) is False

    await payments.release_admin_notification(submission.request.id)
    assert await payments.claim_admin_notification(submission.request.id) is True
    assert await payments.finish_admin_notification(submission.request.id, 98765) is True
    assert await payments.finish_admin_notification(submission.request.id, 99999) is False

    request = await payments.get_request(submission.request.id)
    assert request is not None
    assert request.admin_message_id == 98765


@pytest.mark.asyncio
async def test_single_number_plan_is_not_available(repositories, service):
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=445))

    with pytest.raises(ValueError, match="Only weekly access"):
        await service.begin_payment(
            user_id=301,
            apartment_id=apartment.id,
            username=None,
            first_name="Buyer",
            plan="single",
        )


@pytest.mark.asyncio
async def test_weekly_access_unlocks_every_apartment_and_expires(repositories, service):
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=601))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=602, phone="+996555000002"))
    submission = await service.begin_payment(
        user_id=700,
        apartment_id=first.id,
        username="weekly",
        first_name="Weekly",
        plan=WEEK_PLAN,
    )
    await service.submit_receipt(user_id=700, file_id="weekly-check", file_type="photo")
    assert await service.decide(submission.request.id, approve=True, actor_id=999) == "approved"

    access = await service.contact_status(700, second.id)
    assert access.status == "approved"
    assert access.apartment.id == second.id
    assert access.access_expires_at is not None

    async with sessions.begin() as session:
        await session.execute(
            update(PaymentRequest)
            .where(PaymentRequest.id == submission.request.id)
            .values(access_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    assert (await service.contact_status(700, second.id)).status == "unpaid"


@pytest.mark.asyncio
async def test_published_apartment_blocks_id_and_fingerprint_duplicates(repositories):
    apartments, _, _ = repositories
    original = make_ad(lalafo_id=555)
    apartment = await apartments.upsert_discovered(original)

    assert await apartments.is_duplicate(original) is False
    await apartments.mark_published(apartment.id, chat_id=-100123, message_id=77)

    published = await apartments.get(apartment.id)
    assert published is not None
    assert published.keyboard_version == APARTMENT_KEYBOARD_VERSION
    assert published.phone_source_version == PHONE_SOURCE_VERSION

    assert await apartments.is_duplicate(original) is True
    assert await apartments.is_duplicate(make_ad(lalafo_id=556)) is True
    assert await apartments.is_duplicate(
        make_ad(lalafo_id=557, price=30000, district="Другой район")
    ) is True
    assert await apartments.published_lalafo_ids([555, 999]) == {555}

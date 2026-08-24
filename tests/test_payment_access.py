import pytest

from app.payments.service import PaymentService
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_payment_state_machine(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())

    denied = await service.contact_status(100, apartment.id)
    assert denied.status == "unpaid"

    first = await service.submit_payment(
        user_id=100, apartment_id=apartment.id, username="buyer", first_name="Buyer"
    )
    assert first.outcome == "created"
    duplicate = await service.submit_payment(
        user_id=100, apartment_id=apartment.id, username="buyer", first_name="Buyer"
    )
    assert duplicate.outcome == "pending"
    assert (await service.contact_status(100, apartment.id)).status == "pending"

    assert await service.decide(first.request.id, approve=True, actor_id=111) == "forbidden"
    assert await service.decide(first.request.id, approve=True, actor_id=999) == "approved"
    assert await service.decide(first.request.id, approve=False, actor_id=999) == "already_approved"
    approved = await service.contact_status(100, apartment.id)
    assert approved.status == "approved"
    assert approved.apartment.phone == "+996555123456"
    assert (
        await service.submit_payment(
            user_id=100, apartment_id=apartment.id, username=None, first_name="Buyer"
        )
    ).outcome == "approved"


@pytest.mark.asyncio
async def test_rejection_allows_resubmission(repositories, service):
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=222))
    submission = await service.submit_payment(
        user_id=200, apartment_id=apartment.id, username=None, first_name="No username"
    )
    assert await service.decide(submission.request.id, approve=False, actor_id=999) == "rejected"
    assert (await service.contact_status(200, apartment.id)).status == "rejected"
    retried = await service.submit_payment(
        user_id=200, apartment_id=apartment.id, username=None, first_name="No username"
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
        await service.submit_payment(
            user_id=1, apartment_id=apartment.id, username=None, first_name="User"
        )


@pytest.mark.asyncio
async def test_admin_notification_claim_is_atomic_and_retryable(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=444))
    submission = await service.submit_payment(
        user_id=300, apartment_id=apartment.id, username="buyer", first_name="Buyer"
    )

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
async def test_published_apartment_blocks_id_and_fingerprint_duplicates(repositories):
    apartments, _, _ = repositories
    original = make_ad(lalafo_id=555)
    apartment = await apartments.upsert_discovered(original)

    assert await apartments.is_duplicate(original) is False
    await apartments.mark_published(apartment.id, chat_id=-100123, message_id=77)

    assert await apartments.is_duplicate(original) is True
    assert await apartments.is_duplicate(make_ad(lalafo_id=556)) is True

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

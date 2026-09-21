from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.payments.service import PaymentService
from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import MONTH_PLAN, WEEK_PLAN
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
    assert (await service.contact_status(100, apartment.id)).status == "approved"

    assert await service.decide(first.request.id, approve=True, actor_id=111) == "forbidden"
    assert await service.decide(first.request.id, approve=True, actor_id=999) == "already_approved"
    assert await service.decide(first.request.id, approve=False, actor_id=999) == "already_approved"
    approved = await service.contact_status(100, apartment.id)
    assert approved.status == "approved"
    assert approved.apartment.phone == "+996555123456"
    assert approved.access_expires_at is not None


@pytest.mark.asyncio
async def test_verified_finik_payment_is_approved_without_manual_review(
    repositories, service
):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9191))
    submission = await service.begin_payment(
        user_id=9191,
        apartment_id=apartment.id,
        username="finik_user",
        first_name="Finik",
        plan=WEEK_PLAN,
    )
    request = await payments.prepare_provider_payment(submission.request.id, "payment-9191")
    assert request.provider_payment_id == "payment-9191"

    mismatch, _ = await payments.apply_provider_result(
        "payment-9191", succeeded=True, amount=1
    )
    assert mismatch == "amount_mismatch"
    assert (await service.contact_status(9191, apartment.id)).status == "awaiting_receipt"

    approved_outcome, _ = await payments.apply_provider_result(
        "payment-9191", succeeded=True, amount=499
    )
    assert approved_outcome == "approved"
    approved = await service.contact_status(9191, apartment.id)
    assert approved.status == "approved"
    expiry = approved.access_expires_at
    assert expiry is not None
    repeated, _ = await payments.apply_provider_result(
        "payment-9191", succeeded=True, amount=499
    )
    assert repeated == "already_approved"
    async with repositories[2]() as session:
        history_count = await session.scalar(select(func.count(PaymentHistory.id)))
        history = await session.scalar(select(PaymentHistory))
    assert history_count == 1
    assert history is not None
    assert history.telegram_user_id == 9191
    assert history.plan == WEEK_PLAN
    assert history.provider_payment_id == "payment-9191"
    assert history.access_expires_at == expiry


@pytest.mark.asyncio
async def test_open_checkout_is_reused_for_same_customer_and_plan(
    repositories, service
):
    apartments, payments, _ = repositories
    first_apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9301))
    second_apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9302))
    first = await service.begin_payment(
        user_id=9300,
        apartment_id=first_apartment.id,
        username="same_customer",
        first_name="Same",
        plan=WEEK_PLAN,
    )
    await payments.prepare_provider_payment(first.request.id, "stable-payment")

    second = await service.begin_payment(
        user_id=9300,
        apartment_id=second_apartment.id,
        username="same_customer",
        first_name="Same",
        plan=WEEK_PLAN,
    )

    assert second.request.id == first.request.id
    assert second.request.provider_payment_id == "stable-payment"


@pytest.mark.asyncio
async def test_finik_link_is_rotated_after_merchant_change(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9292))
    submission = await service.begin_payment(
        user_id=9292,
        apartment_id=apartment.id,
        username="merchant_migration",
        first_name="Migration",
        plan=WEEK_PLAN,
    )
    old = await payments.prepare_provider_payment(
        submission.request.id,
        "old-payment",
        configuration_id="old-account",
    )
    await payments.set_provider_payment_url(
        old.id,
        "https://qr.finik.kg/old-link",
        configuration_id="old-account",
    )

    rotated = await payments.prepare_provider_payment(
        old.id,
        "new-payment",
        configuration_id="corporate-account",
    )
    assert rotated.provider_payment_id == "new-payment"
    assert rotated.provider_payment_url is None
    assert rotated.provider_status == "created:corporate-account"

    repeated = await payments.prepare_provider_payment(
        old.id,
        "must-not-replace",
        configuration_id="corporate-account",
    )
    assert repeated.provider_payment_id == "new-payment"


@pytest.mark.asyncio
async def test_submitted_request_cannot_be_rejected_after_auto_approval(repositories, service):
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
    assert await service.decide(
        submission.request.id, approve=False, actor_id=999
    ) == "already_approved"
    assert (await service.contact_status(200, apartment.id)).status == "approved"


@pytest.mark.asyncio
async def test_missing_apartment_is_denied_but_inactive_card_remains_payable(
    repositories, service
):
    apartments, _, _ = repositories
    assert (await service.contact_status(1, 999999)).status == "unavailable"
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=333))
    await apartments.mark_inactive(apartment.id)
    assert (await service.contact_status(1, apartment.id)).status == "unpaid"
    submission = await service.begin_payment(
        user_id=1,
        apartment_id=apartment.id,
        username=None,
        first_name="User",
        plan=WEEK_PLAN,
    )
    assert submission.outcome == "created"


@pytest.mark.asyncio
async def test_auto_approved_submission_does_not_notify_admin(repositories, service):
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

    assert await payments.claim_admin_notification(submission.request.id) is False

    request = await payments.get_request(submission.request.id)
    assert request is not None
    assert request.status == "approved"
    assert request.admin_message_id is None


@pytest.mark.asyncio
async def test_miniapp_payment_claim_auto_approves_once(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=446))
    submission = await service.begin_payment(
        user_id=302,
        apartment_id=apartment.id,
        username="buyer",
        first_name="Buyer",
        plan=WEEK_PLAN,
    )

    first = await payments.mark_payment_claimed(
        user_id=302, apartment_id=apartment.id
    )
    repeated = await payments.mark_payment_claimed(
        user_id=302, apartment_id=apartment.id
    )

    assert first is not None
    assert first.id == submission.request.id
    assert first.status == "approved"
    assert first.receipt_file_id is None
    assert repeated is not None
    assert repeated.id == first.id
    assert repeated.status == "approved"


@pytest.mark.asyncio
async def test_single_number_plan_is_not_available(repositories, service):
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=445))

    with pytest.raises(ValueError, match="Unsupported access plan"):
        await service.begin_payment(
            user_id=301,
            apartment_id=apartment.id,
            username=None,
            first_name="Buyer",
            plan="single",
        )


@pytest.mark.asyncio
async def test_monthly_access_unlocks_all_apartments(repositories, service):
    apartments, payments, _ = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=451))
    second = await apartments.upsert_discovered(
        make_ad(lalafo_id=452, phone="+996555000452")
    )
    submission = await service.begin_payment(
        user_id=451,
        apartment_id=first.id,
        username="monthly",
        first_name="Monthly",
        plan=MONTH_PLAN,
    )
    await payments.mark_payment_claimed(user_id=451, apartment_id=first.id)
    assert await service.decide(
        submission.request.id, approve=True, actor_id=999
    ) == "already_approved"
    access = await service.contact_status(451, second.id)
    assert access.status == "approved"
    assert access.plan == MONTH_PLAN
    assert access.access_expires_at is not None


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
    assert await service.decide(
        submission.request.id, approve=True, actor_id=999
    ) == "already_approved"

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
    # One realtor phone may legitimately represent several different units.
    assert await apartments.is_duplicate(
        make_ad(lalafo_id=557, price=30000, district="Другой район")
    ) is False
    assert await apartments.published_lalafo_ids([555, 999]) == {555}
    assert await apartments.repostable_lalafo_ids([555, 999], after_hours=0) == {555}
    repostable = await apartments.repostable_lalafo_publications(
        [555, 999], after_hours=0
    )
    assert set(repostable) == {555}
    assert repostable[555] is not None
    assert await apartments.repostable_lalafo_ids([555, 999], after_hours=24) == set()

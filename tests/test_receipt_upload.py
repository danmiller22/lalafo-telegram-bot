from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.bot.handlers import ReceiptUpload, receipt_handler, start_handler
from app.config import Settings
from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import LIFETIME_PLAN
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "document"])
async def test_uploaded_receipt_unlocks_contacts_and_is_saved_once(repositories, service, monkeypatch, kind):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9911))
    other = await apartments.upsert_discovered(make_ad(lalafo_id=9912))
    checkout = await service.begin_payment(user_id=901, apartment_id=apartment.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    await service.begin_payment(user_id=901, apartment_id=other.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    delivery = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.send_private_contact", delivery)
    state = SimpleNamespace(get_data=AsyncMock(side_effect=[{"receipt_apartment_id": apartment.id}, {}]), clear=AsyncMock())
    message = SimpleNamespace(from_user=SimpleNamespace(id=901), answer=AsyncMock(),
        photo=[SimpleNamespace(file_id="telegram-receipt-photo")] if kind == "photo" else [],
        document=SimpleNamespace(file_id="telegram-receipt-pdf", mime_type="application/pdf") if kind == "document" else None)
    await receipt_handler(message, payments, service, Settings(), object(), state)
    stored = await payments.get_request(checkout.request.id)
    assert stored.status == "approved"
    assert stored.receipt_file_id == f"telegram-receipt-{'photo' if kind == 'photo' else 'pdf'}"
    assert stored.receipt_file_type == kind
    assert (await service.contact_status(901, other.id)).status == "approved"
    delivery.assert_awaited_once()
    assert delivery.await_args.kwargs["user_id"] == 901
    assert delivery.await_args.kwargs["apartment"].id == apartment.id
    # Repeated uploads do not create additional access grants or paid history.
    await receipt_handler(message, payments, service, Settings(), object(), state)
    delivery.assert_awaited_once()
    async with sessions() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == 1
        assert (await session.scalar(select(PaymentRequest).where(PaymentRequest.apartment_id == other.id))).receipt_file_id is None


@pytest.mark.asyncio
async def test_invalid_document_cannot_unlock_access(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9913))
    await service.begin_payment(user_id=902, apartment_id=apartment.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    state = SimpleNamespace(get_data=AsyncMock(), clear=AsyncMock())
    message = SimpleNamespace(from_user=SimpleNamespace(id=902), answer=AsyncMock(), photo=[],
        document=SimpleNamespace(file_id="bad-file", mime_type="application/zip"))
    await receipt_handler(message, payments, service, Settings(), object(), state)
    assert (await service.contact_status(902, apartment.id)).status == "awaiting_receipt"
    state.get_data.assert_not_awaited()
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_requires_checkout_and_cannot_target_another_user(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9914))
    assert await payments.submit_receipt(user_id=903, file_id="receipt", file_type="photo", apartment_id=apartment.id) is None
    await service.begin_payment(user_id=904, apartment_id=apartment.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    assert await payments.submit_receipt(user_id=903, file_id="receipt", file_type="photo", apartment_id=apartment.id) is None
    assert (await service.contact_status(904, apartment.id)).status == "awaiting_receipt"
    with pytest.raises(ValueError):
        await payments.submit_receipt(user_id=904, file_id="", file_type="photo")


@pytest.mark.asyncio
async def test_legacy_pending_claim_does_not_unlock_access(repositories, service):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9915))
    checkout = await service.begin_payment(user_id=905, apartment_id=apartment.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == checkout.request.id).values(status="pending"))
    assert (await payments.mark_payment_claimed(user_id=905, apartment_id=apartment.id)).status == "pending"
    assert (await service.contact_status(905, apartment.id)).status == "pending"
    await payments.submit_receipt(user_id=905, file_id="receipt", file_type="photo", apartment_id=apartment.id)
    assert (await service.contact_status(905, apartment.id)).status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_receipt_start_link_selects_exact_checkout(repositories, service, valid):
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9916))
    await service.begin_payment(user_id=906, apartment_id=apartment.id,
        username=None, first_name="Test", plan=LIFETIME_PLAN)
    signer = TokenSigner("receipt-test-secret-long")
    token = signer.sign_start_id("receipt", apartment.id) if valid else "invalid"
    message = SimpleNamespace(text=f"/start receipt_{token}", from_user=SimpleNamespace(id=906), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock(), update_data=AsyncMock())
    await start_handler(message, service, signer, Settings(), object(), state)
    if valid:
        state.set_state.assert_awaited_once_with(ReceiptUpload.waiting)
        state.update_data.assert_awaited_once_with(receipt_apartment_id=apartment.id)
        assert "PDF" in message.answer.await_args.args[0]
    else:
        state.set_state.assert_not_awaited()
    assert (await service.contact_status(906, apartment.id)).status == "awaiting_receipt"

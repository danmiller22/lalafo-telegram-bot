"""Regression: ordinary private media must never enter a payment receipt flow."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Document, Message, PhotoSize, User

from app.bot import handlers
from app.config import Settings
from app.payment_plans import WEEK_PLAN
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("has_checkout", [False, True])
async def test_private_media_is_not_treated_as_payment_receipt(repositories, service, kind, has_checkout):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9911))
    if has_checkout:
        await service.begin_payment(user_id=901, apartment_id=apartment.id,
            username=None, first_name="Test", plan=WEEK_PLAN)
    message = Message(message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=901, type="private"), from_user=User(id=901, is_bot=False, first_name="Test"),
        photo=[PhotoSize(file_id="photo", file_unique_id="photo-unique", width=10, height=10)] if kind == "photo" else None,
        document=Document(file_id="pdf", file_unique_id="pdf-unique", mime_type="application/pdf") if kind == "document" else None)
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock(), update_data=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    result = await handlers.router.propagate_event("message", message,
        payments=payments, service=service, state=state, bot=bot,
        settings=Settings(), signer=TokenSigner("receipt-disabled-secret-long"))
    assert result is UNHANDLED
    bot.send_message.assert_not_awaited()
    state.set_state.assert_not_awaited()
    if has_checkout:
        stored = await payments.get_access(901, apartment.id)
        assert stored.receipt_file_id is None
        assert stored.payment_claimed_at is None
        assert stored.status == "awaiting_receipt"
    else:
        assert await payments.get_access(901, apartment.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_old_receipt_link_returns_to_menu_without_upload_prompt(repositories, service, valid):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=9912))
    await service.begin_payment(user_id=902, apartment_id=apartment.id,
        username=None, first_name="Test", plan=WEEK_PLAN)
    signer = TokenSigner("receipt-disabled-secret-long")
    token = signer.sign_start_id("receipt", apartment.id) if valid else "invalid"
    message = SimpleNamespace(text=f"/start receipt_{token}",
        from_user=SimpleNamespace(id=902), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock(), update_data=AsyncMock())
    await handlers.start_handler(message, service, signer, Settings(), object(), state)
    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
    assert "чек" not in message.answer.await_args.args[0].lower()
    assert (await payments.get_access(902, apartment.id)).status == "awaiting_receipt"


def test_old_deployment_qr_is_replaced_with_requested_shared_link(monkeypatch):
    legacy = (
        "https://qr.finik.kg/#00020101021232810011qr.finik.kg0114averspay-items"
        "1032bbfd79c838a6483eb57bc42ea362fa811202121302125204482953034175405"
        "500005908Finik-QR6304a55c"
    )
    monkeypatch.setenv("WEEKLY_FINIK_PAYMENT_URL", legacy)
    settings = Settings()
    assert settings.weekly_finik_payment_url == "https://qr.finik.kg/bbfd79c8-38a6-483e-b57b-c42ea362fa81?type=t"
    # Future explicit configuration must keep working.
    assert Settings(weekly_finik_payment_url="https://qr.finik.kg/custom").weekly_finik_payment_url.endswith("/custom")

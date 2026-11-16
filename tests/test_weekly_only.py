from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import web
from app.bot.handlers import plan_handler
from app.config import Settings
from app.payment_plans import WEEK_FINIK_QR
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_bot_weekly_uses_request_bound_finik_despite_shared_qr(repositories, service, monkeypatch):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    settings = Settings(_env_file=None, weekly_finik_payment_url=WEEK_FINIK_QR,
        finik_api_key="configured", finik_account_id="configured", finik_private_key_pem="configured",
        telegram_webhook_url="https://example.test/telegram/webhook")
    signer = TokenSigner("weekly-only-test-secret")
    callback = SimpleNamespace(data=f"plan:w:{signer.sign_id('plan-week', apartment.id)}",
        from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
        answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    checkout = AsyncMock(return_value="https://qr.finik.kg/customer-bound")
    monkeypatch.setattr(web, "_finik_checkout_url", checkout)
    await plan_handler(callback, service, signer, settings, object())
    checkout.assert_awaited_once()
    assert checkout.await_args.kwargs["plan"] == "week"
    row = await payments.get_access(123, apartment.id)
    assert row.plan == "week" and row.status == "awaiting_receipt"
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://qr.finik.kg/customer-bound"
    assert "499" in markup.inline_keyboard[0][0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["lifetime", "month", "personal"])
async def test_prepare_rejects_retired_plans_before_invoice_or_database_write(monkeypatch, plan):
    service = SimpleNamespace(contact_status=AsyncMock())
    payments = SimpleNamespace(submit=AsyncMock())
    runtime = SimpleNamespace(workflow_data={"service": service, "payments": payments})
    monkeypatch.setattr(web, "_miniapp_context", lambda payload: (Settings(), runtime, SimpleNamespace(id=123), 1))
    with pytest.raises(HTTPException) as error:
        await web.miniapp_prepare_payment(web.MiniAppRequest(init_data="test", start_param="test", plan=plan))
    assert error.value.status_code == 400
    payments.submit.assert_not_awaited()
    service.contact_status.assert_not_awaited()

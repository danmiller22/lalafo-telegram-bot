from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import web
from app.finik import payment_configuration_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "expired", "wrong_merchant", "failed", "future"])
async def test_cached_checkout_only_skips_reads_when_still_valid(case):
    settings = SimpleNamespace(finik_auto_enabled=True, finik_api_url="https://example.com/pay", finik_account_id="merchant")
    configuration = payment_configuration_id(api_url=settings.finik_api_url, account_id=settings.finik_account_id)
    request = SimpleNamespace(
        plan="week", status="awaiting_receipt", telegram_user_id=42,
        created_at=datetime.now(UTC) - timedelta(seconds=30),
        provider_status=f"waiting:{configuration}",
        provider_payment_url="https://qr.finik.kg/prepared",
    )
    if case == "expired":
        request.created_at -= timedelta(minutes=5)
    elif case == "future":
        request.created_at += timedelta(minutes=5)
    elif case == "wrong_merchant":
        request.provider_status = "waiting:old"
    elif case == "failed":
        request.provider_status = "failed"
    payments = SimpleNamespace(reusable_provider_checkout=AsyncMock(side_effect=LookupError("normal checkout path")))
    if case == "valid":
        assert await web._finik_checkout_url(settings, payments, request, apartment_id=1, plan="week") == request.provider_payment_url
        payments.reusable_provider_checkout.assert_not_awaited()
    else:
        with pytest.raises(LookupError, match="normal checkout path"):
            await web._finik_checkout_url(settings, payments, request, apartment_id=1, plan="week")
        payments.reusable_provider_checkout.assert_awaited_once()

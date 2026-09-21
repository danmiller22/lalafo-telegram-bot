from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import receipt_handler
from app.security import TokenSigner


@pytest.mark.asyncio
async def test_receipt_is_auto_approved_without_admin_notification():
    request = SimpleNamespace(
        id=17,
        telegram_user_id=555,
        username="buyer",
        first_name="Buyer",
        plan="week",
        status="approved",
        apartment=SimpleNamespace(id=3, district="ЦУМ", city="Бишкек"),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        document=None,
        answer=AsyncMock(),
    )
    service = SimpleNamespace(submit_receipt=AsyncMock(return_value=request))
    payments = SimpleNamespace(
        claim_admin_notification=AsyncMock(return_value=True),
        finish_admin_notification=AsyncMock(),
        release_admin_notification=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=99)),
        send_document=AsyncMock(),
    )
    settings = SimpleNamespace(admin_user_id=999)

    await receipt_handler(
        message,
        service,
        payments,
        TokenSigner("a-very-long-test-secret"),
        settings,
        bot,
    )

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    payments.claim_admin_notification.assert_not_awaited()
    payments.finish_admin_notification.assert_not_awaited()

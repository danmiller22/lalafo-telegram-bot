from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import receipt_handler
from app.security import TokenSigner


@pytest.mark.asyncio
async def test_receipt_is_forwarded_to_admin_with_payer_and_plan():
    request = SimpleNamespace(
        id=17,
        telegram_user_id=555,
        username="buyer",
        first_name="Buyer",
        plan="week",
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

    bot.send_photo.assert_awaited_once()
    args = bot.send_photo.await_args
    assert args.args[:2] == (999, "large")
    assert "@buyer (ID 555)" in args.kwargs["caption"]
    assert "Доступ на неделю" in args.kwargs["caption"]
    assert "500 сом" in args.kwargs["caption"]
    payments.finish_admin_notification.assert_awaited_once_with(17, 99)

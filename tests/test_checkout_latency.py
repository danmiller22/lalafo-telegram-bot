from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.config import Settings
from app.models import XPayAttempt
from app.payment_plans import WEEK_PLAN
from app.xpay import XPayClient, create_checkout
from tests.helpers import make_ad

@pytest.mark.asyncio
async def test_reuses_only_unexpired_xpay_checkout(repositories, monkeypatch):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    request = (await payments.submit(user_id=19, apartment_id=apartment.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    settings = Settings(_env_file=None, xpay_client_id='id', xpay_client_secret='secret', xpay_service_uuid='uuid', telegram_webhook_url='https://example.test/telegram/webhook')
    create = AsyncMock(side_effect=[('Tx1', 'https://pay.xpay.kg/1'), ('Tx2', 'https://pay.xpay.kg/2')])
    monkeypatch.setattr(XPayClient, 'create', create)
    assert (await create_checkout(sessions, settings, request_id=request.id))[0].endswith('/1')
    assert (await create_checkout(sessions, settings, request_id=request.id))[0].endswith('/1')
    async with sessions.begin() as session:
        attempt = await session.scalar(select(XPayAttempt))
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert (await create_checkout(sessions, settings, request_id=request.id))[0].endswith('/2')
    assert create.await_count == 2

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from app.config import Settings
from app.models import PaymentHistory, XPayAttempt, WantedAd
from app.payment_plans import WEEK_PLAN
from app.xpay import create_checkout, reconcile, XPayClient
from tests.helpers import make_ad


def settings():
    return Settings(_env_file=None, xpay_client_id='id', xpay_client_secret='secret',
        xpay_service_uuid='uuid', telegram_webhook_url='https://example.test/telegram/webhook')


@pytest.mark.asyncio
async def test_contact_payment_checked_and_granted_once(repositories, service, monkeypatch):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await payments.submit(user_id=100, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    create = AsyncMock(return_value=('Provider123', 'https://pay.xpay.kg/qr'))
    monkeypatch.setattr(XPayClient, 'create', create)
    url, expires = await create_checkout(sessions, settings(), request_id=submission.request.id)
    assert url == 'https://pay.xpay.kg/qr'
    assert timedelta(minutes=4, seconds=50) < expires - datetime.now(timezone.utc) <= timedelta(minutes=5)
    assert (await create_checkout(sessions, settings(), request_id=submission.request.id))[0] == url
    assert create.await_count == 1
    async with sessions() as session:
        attempt = await session.scalar(select(XPayAttempt))
        token = attempt.token
    status = AsyncMock(return_value={'qr_transaction_id':'Provider123', 'pay_status':'COMPLETED',
        'request_amount':'398.00','amount':'399.00','transaction_uuid':'uuid'})
    monkeypatch.setattr(XPayClient, 'status', status)
    assert (await reconcile(sessions, settings(), token))[0] == 'amount_mismatch'
    assert (await service.contact_status(100, apartment.id)).status != 'approved'
    status.return_value['request_amount'] = '399.00'
    assert (await reconcile(sessions, settings(), token))[0] == 'approved'
    assert (await reconcile(sessions, settings(), token))[0] == 'already_paid'
    result = await service.contact_status(100, apartment.id)
    assert result.status == 'approved' and result.access_expires_at is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentHistory)) == 1


@pytest.mark.asyncio
async def test_search_request_remains_unpaid_until_xpay_completed(repositories, monkeypatch):
    _, _, sessions = repositories
    async with sessions.begin() as session:
        ad = WantedAd(telegram_user_id=101, rooms='1', district='ЦУМ', budget=40000,
            move_in='сейчас', tenants='1', notes='нет', contact='telegram', status='awaiting_payment')
        session.add(ad)
        await session.flush()
        ad_id = ad.id
    monkeypatch.setattr(XPayClient, 'create', AsyncMock(return_value=('Search123','https://pay.xpay.kg/qr')))
    await create_checkout(sessions, settings(), wanted_ad_id=ad_id)
    async with sessions() as session:
        attempt = await session.scalar(select(XPayAttempt))
        token = attempt.token
        assert attempt.expected_amount == 100
    status = AsyncMock(return_value={'qr_transaction_id':'Search123', 'pay_status':'PROCESSING',
        'request_amount':'100','amount':'100','transaction_uuid':'uuid'})
    monkeypatch.setattr(XPayClient, 'status', status)
    assert (await reconcile(sessions, settings(), token))[0] == 'waiting'
    async with sessions() as session:
        assert (await session.get(WantedAd, ad_id)).status == 'awaiting_payment'
    status.return_value['pay_status'] = 'COMPLETED'
    assert (await reconcile(sessions, settings(), token))[0] == 'approved'
    assert (await reconcile(sessions, settings(), token))[0] == 'already_paid'
    async with sessions() as session:
        assert (await session.get(WantedAd, ad_id)).status == 'paid'

@pytest.mark.asyncio
async def test_checkout_is_shared_across_apartments_for_same_buyer(repositories, monkeypatch):
    apartments, payments, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=800001))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=800002))
    req1 = (await payments.submit(user_id=100, apartment_id=first.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    req2 = (await payments.submit(user_id=100, apartment_id=second.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    create = AsyncMock(return_value=('Same123', 'https://pay.xpay.kg/qr'))
    monkeypatch.setattr(XPayClient, 'create', create)
    a = await create_checkout(sessions, settings(), request_id=req1.id)
    b = await create_checkout(sessions, settings(), request_id=req2.id)
    assert a == b and create.await_count == 1

@pytest.mark.asyncio
async def test_xpay_check_url_enforces_five_minute_expiry_and_prevents_repayment(repositories, monkeypatch):
    from fastapi import HTTPException
    from app import web
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    request = (await payments.submit(user_id=100, apartment_id=apartment.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add(XPayAttempt(token='xpay-token', request_id=request.id, provider_id='Provider123',
            expected_amount=399, status='waiting', created_at=now, expires_at=now+timedelta(minutes=5)))
    monkeypatch.setattr(web, '_bot_runtime', SimpleNamespace(workflow_data={'payments':payments}))
    monkeypatch.setattr(web, 'get_settings', lambda: settings())
    assert (await web.xpay_check('xpay-token')).status_code == 201
    async with sessions.begin() as session:
        attempt = await session.scalar(select(XPayAttempt).where(XPayAttempt.token == 'xpay-token'))
        attempt.expires_at = now - timedelta(seconds=1)
    with pytest.raises(HTTPException) as exc:
        await web.xpay_check('xpay-token')
    assert exc.value.status_code == 403

@pytest.mark.asyncio
async def test_api_creates_exact_nonchangeable_dynamic_qr(monkeypatch):
    import httpx
    import json
    seen = []
    async def handler(request):
        seen.append(request)
        if request.url.path.endswith('/developer/login'):
            return httpx.Response(200, json={'status':'Success','data':{'access_token':'token', 'service':[{'uuid':'uuid','currency_code':'KGS'}]}})
        assert request.headers['Authorization'] == 'Bearer token'
        body = json.loads(request.content)
        assert body['uuid'] == 'uuid' and body['amount'] == 39900
        assert body['type'] == 'dynamic' and body['amount_change'] is False
        assert body['check_url'].endswith('/check/token') and body['callback_url'].endswith('/webhook/token')
        return httpx.Response(200, json={'status':'Success','data':{'qr_transaction_id':'Tx123',
            'qr_code':'https://pay.xpay.kg/#qr', 'amount':39900,'request_amount':39900,
            'type':'dynamic','amount_change':False}})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.xpay.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler)))
    assert await XPayClient(settings()).create(amount=399,payer_id=10,
        callback_url='https://example.test/webhook/token',check_url='https://example.test/check/token') == ('Tx123','https://pay.xpay.kg/#qr')
    assert len(seen) == 2

def test_vault_encrypts_credentials_and_rejects_wrong_database_key():
    import json
    from cryptography.fernet import InvalidToken
    from app.xpay_vault import encrypt_credentials, cipher
    data = {'client_id':'secret-id','client_secret':'secret-value','uuid':'service'}
    encrypted = encrypt_credentials('postgresql://user:password@db/test', data)
    assert 'secret-value' not in encrypted
    assert json.loads(cipher('postgresql://user:password@db/test').decrypt(encrypted.encode())) == data
    with pytest.raises(InvalidToken):
        cipher('postgresql://user:wrong@db/test').decrypt(encrypted.encode())

@pytest.mark.asyncio
async def test_untrusted_webhook_does_not_grant_without_provider_confirmation(repositories, service, monkeypatch):
    from app import web
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    request = (await payments.submit(user_id=81, apartment_id=apartment.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add(XPayAttempt(token='forged-callback-test', request_id=request.id,
            provider_id='Transaction123', expected_amount=399, status='waiting',
            created_at=now, expires_at=now+timedelta(minutes=5)))
    monkeypatch.setattr(web, '_bot_runtime', SimpleNamespace(workflow_data={'payments':payments}))
    monkeypatch.setattr(web, 'get_settings', lambda: settings())
    monkeypatch.setattr(XPayClient, 'status', AsyncMock(return_value={'qr_transaction_id':'Transaction123', 'pay_status':'WAITING'}))
    result = await web.xpay_webhook('forged-callback-test')
    assert result == {'status':'waiting'}
    assert (await service.contact_status(81, apartment.id)).status != 'approved'

@pytest.mark.asyncio
async def test_previous_finik_checkout_is_not_replaced_while_payable(repositories, monkeypatch):
    from app.xpay import XPayError
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    request = (await payments.submit(user_id=90, apartment_id=apartment.id, username=None, first_name=None, plan=WEEK_PLAN)).request
    await payments.prepare_provider_payment(request.id, 'old-finik', configuration_id='old')
    await payments.set_provider_payment_url(request.id, 'https://qr.finik.kg/old', configuration_id='old', payment_id='old-finik')
    provider = AsyncMock()
    monkeypatch.setattr(XPayClient, 'create', provider)
    with pytest.raises(XPayError, match='Previous checkout'):
        await create_checkout(sessions, settings(), request_id=request.id)
    provider.assert_not_awaited()

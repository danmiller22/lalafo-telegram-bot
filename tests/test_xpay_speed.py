from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import app.xpay as xpay
from app.config import Settings
from app.models import XPayAttempt
from app.payment_plans import LIFETIME_PLAN, WEEKLY_PLAN
from app.xpay import XPayClient, create_checkout, discard_unopened_checkout
from tests.helpers import make_ad


def _settings():
    return Settings(_env_file=None, xpay_client_id='id', xpay_client_secret='secret', xpay_service_uuid='uuid',
                    telegram_webhook_url='https://example.test/telegram/webhook')


def _qr(amount=49900):
    return {'status': 'Success', 'data': {'type': 'dynamic', 'amount_change': False, 'qr_transaction_id': 'Tx1',
            'pay_link': 'https://pay.xpay.kg/1', 'amount': amount, 'request_amount': amount}}


@pytest.fixture
def xpay_http(monkeypatch):
    xpay._tokens.clear()
    state = {'logins': 0, 'qr': 0, 'reject_first_qr': False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/login'):
            state['logins'] += 1
            return httpx.Response(200, json={'status': 'Success', 'data': {
                'access_token': f"t{state['logins']}", 'service': [{'uuid': 'uuid', 'currency_code': 'KGS'}]}})
        state['qr'] += 1
        if state['reject_first_qr'] and state['qr'] == 1:
            return httpx.Response(401, json={'message': 'Unauthenticated'})
        state.setdefault('tokens', []).append(request.headers['authorization'])
        return httpx.Response(200, json=_qr())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(xpay, '_shared_client', lambda: client)
    yield state
    xpay._tokens.clear()


@pytest.mark.asyncio
async def test_token_is_reused_across_checkouts(xpay_http):
    kwargs = dict(amount=499, callback_url='https://x/cb', check_url='https://x/ck', payer_id=1,
                  service_name='Доступ на 7 дней Rentify', comments='Оплата доступа Rentify на 7 дней')
    client = XPayClient(_settings())
    for _ in range(3):
        assert await client.create(**kwargs) == ('Tx1', 'https://pay.xpay.kg/1')
    assert xpay_http['logins'] == 1 and xpay_http['qr'] == 3


@pytest.mark.asyncio
async def test_rejected_token_is_refreshed_once(xpay_http):
    xpay_http['reject_first_qr'] = True
    client = XPayClient(_settings())
    await client.create(amount=499, callback_url='https://x/cb', check_url='https://x/ck', payer_id=1,
                        service_name='Доступ на 7 дней Rentify', comments='Оплата доступа Rentify на 7 дней')
    assert xpay_http['logins'] == 2
    assert xpay_http['tokens'] == ['Bearer t2']


@pytest.mark.asyncio
async def test_warm_logs_in_once(xpay_http):
    client = XPayClient(_settings())
    await client.warm()
    await client.warm()
    assert xpay_http['logins'] == 1


@pytest.mark.asyncio
async def test_discard_only_closes_unopened_prefetched_checkout(repositories, monkeypatch):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    monkeypatch.setattr(XPayClient, 'create', AsyncMock(side_effect=[('Tx1', 'https://pay.xpay.kg/1'), ('Tx2', 'https://pay.xpay.kg/2')]))
    monkeypatch.setattr(XPayClient, 'status', AsyncMock(return_value={'pay_status': 'WAITING'}))
    settings = _settings()
    # Prefetched (request left "prepared"): another tariff is blocked until it is discarded.
    prepared = (await payments.submit(user_id=7, apartment_id=apartment.id, username=None, first_name=None,
                                      plan=WEEKLY_PLAN, prepare_only=True)).request
    await create_checkout(sessions, settings, request_id=prepared.id, expected_plan=WEEKLY_PLAN)
    assert await discard_unopened_checkout(sessions, user_id=7, plan=LIFETIME_PLAN) is False
    assert await discard_unopened_checkout(sessions, user_id=7, plan=WEEKLY_PLAN) is True
    async with sessions() as session:
        assert (await session.scalar(select(XPayAttempt.status))) == 'closed'
    # Once /start has run (awaiting_receipt) the link may have been opened: keep it.
    await payments.submit(user_id=8, apartment_id=apartment.id, username=None, first_name=None,
                          plan=WEEKLY_PLAN, prepare_only=True)
    request = (await payments.submit(user_id=8, apartment_id=apartment.id, username=None, first_name=None,
                                     plan=WEEKLY_PLAN)).request
    await create_checkout(sessions, settings, request_id=request.id, expected_plan=WEEKLY_PLAN)
    assert await discard_unopened_checkout(sessions, user_id=8, plan=WEEKLY_PLAN) is False

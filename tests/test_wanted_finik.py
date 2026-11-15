from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.wanted.repository import WantedAdRepository
from app.wanted.finik import create_checkout, apply_result, publish_paid
from app.models import WantedPayment
from app.config import Settings
from sqlalchemy import select


@pytest.mark.asyncio
async def test_exact_payment_and_duplicate_callback_publish_once(repositories):
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await repo.create(user_id=123, username=None, first_name='A', rooms='1',
        district='Центр', budget=30000, move_in='сейчас', tenants='1', notes='', contact='@test')
    async with sessions.begin() as session:
        session.add(WantedPayment(wanted_ad_id=ad.id, provider_payment_id='wanted-test', status='created'))
    assert (await apply_result(repo, 'wanted-test', True, 99))[0] == 'amount_mismatch'
    assert (await repo.get(ad.id)).status == 'awaiting_payment'
    assert (await apply_result(repo, 'wanted-test', True, 100))[0] == 'approved'
    assert (await apply_result(repo, 'wanted-test', True, 100))[0] == 'already_paid'
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)))
    settings = Settings(telegram_group_id=-1001)
    await publish_paid(repo, settings, bot, ad.id)
    await publish_paid(repo, settings, bot, ad.id)
    assert (await repo.get(ad.id)).status == 'published'
    assert bot.send_message.await_count == 2  # group publication and customer notice


@pytest.mark.asyncio
async def test_ambiguous_telegram_send_is_not_retried(repositories):
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await repo.create(user_id=124, username=None, first_name='A', rooms='1',
        district='Центр', budget=30000, move_in='сейчас', tenants='1', notes='', contact='@test')
    async with sessions.begin() as session:
        session.add(WantedPayment(wanted_ad_id=ad.id, provider_payment_id='wanted-ambiguous', status='created'))
    await apply_result(repo, 'wanted-ambiguous', True, 100)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
    settings = Settings(telegram_group_id=-1001)
    await publish_paid(repo, settings, bot, ad.id)
    await publish_paid(repo, settings, bot, ad.id)
    assert (await repo.get(ad.id)).status == 'publishing'
    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_signed_webhook_publishes_only_after_exact_payment(repositories, monkeypatch):
    import time
    import httpx
    from app import web
    from app.config import get_settings
    from app.finik import canonical_request, sign_request
    from tests.test_finik import _keys
    private, public = _keys()
    for key, value in {'RUN_BOT':'true', 'FINIK_API_KEY':'configured',
        'FINIK_ACCOUNT_ID':'merchant', 'FINIK_PRIVATE_KEY_PEM':private.decode() if isinstance(private, bytes) else private,
        'FINIK_WEBHOOK_PUBLIC_KEY_PEM':public.decode() if isinstance(public, bytes) else public,
        'TELEGRAM_WEBHOOK_URL':'https://example.test/telegram/webhook'}.items():
        monkeypatch.setenv(key,value)
    get_settings.cache_clear()
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await repo.create(user_id=125, username=None, first_name='A', rooms='1',
        district='Центр', budget=30000, move_in='сейчас', tenants='1', notes='', contact='@test')
    async with sessions.begin() as session:
        session.add(WantedPayment(wanted_ad_id=ad.id, provider_payment_id='wanted-signed', status='created'))
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=102)))
    monkeypatch.setattr(web, '_bot_runtime', SimpleNamespace(bot=bot, workflow_data={'wanted_ads':repo}))
    ts=str(int(time.time()*1000)); body={'status':'success','amount':100,'fields':{'paymentId':'wanted-signed'}}
    signature=sign_request(canonical_request(method='POST',path='/finik/webhook',host='test',
        headers={'x-api-timestamp':ts},body=body),private)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://test') as client:
        bad=await client.post('/finik/webhook',headers={'signature':'invalid','x-api-timestamp':ts},json=body)
        assert bad.status_code==401 and (await repo.get(ad.id)).status=='awaiting_payment'
        headers={'signature':signature,'x-api-timestamp':ts}
        first=await client.post('/finik/webhook',headers=headers,json=body)
        second=await client.post('/finik/webhook',headers=headers,json=body)
    assert first.json()['status']=='approved' and second.json()['status']=='already_paid'
    assert (await repo.get(ad.id)).status=='published'
    assert bot.send_message.await_count==2
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_checkout_is_unique_to_request_and_reused(repositories, monkeypatch):
    from unittest.mock import patch
    from app.wanted import finik
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await repo.create(user_id=126, username=None, first_name='A', rooms='1',
        district='Центр', budget=30000, move_in='сейчас', tenants='1', notes='', contact='@test')
    created=AsyncMock(return_value=SimpleNamespace(url='https://qr.finik.kg/checkout'))
    monkeypatch.setattr(finik,'FinikClient',lambda **kw:SimpleNamespace(create_payment=created))
    monkeypatch.setattr(finik,'decode_private_key',lambda **kw:'key')
    settings=SimpleNamespace(finik_api_url='https://example.test',finik_api_key='key',
        finik_account_id='merchant',finik_private_key_pem='key',finik_private_key_b64='',
        telegram_bot_username='testbot',require_public_base_url=lambda:'https://example.test')
    first=await create_checkout(repo,settings,ad)
    second=await create_checkout(repo,settings,ad)
    assert first==second=='https://qr.finik.kg/checkout'
    assert created.await_count==1 and created.await_args.kwargs['amount']==100
    async with sessions() as session:
        payment=await session.scalar(select(WantedPayment).where(WantedPayment.wanted_ad_id==ad.id))
        assert payment.provider_payment_id==created.await_args.kwargs['payment_id']
        assert payment.provider_payment_id.startswith(f'wanted-{ad.id}-')

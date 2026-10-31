from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import BackgroundTasks, HTTPException
from app import web
from app.config import Settings
from app.payments.service import ContactResult


def test_legacy_lifetime_link_is_replaced_by_verified_999_link():
    settings=Settings(_env_file=None,lifetime_finik_payment_url='https://qr.finik.kg/e0c9972e-0f05-4dc3-99fd-96ec1debea1f?type=t')
    assert settings.lifetime_finik_payment_url=='https://qr.finik.kg/08fdd246-0b74-40c6-a37d-29404ff54fa9?type=t'


@pytest.mark.asyncio
@pytest.mark.parametrize('plan,price', [('week',499),('lifetime',999)])
async def test_checkout_keeps_selected_plan_and_price(monkeypatch,plan,price):
    settings=Settings(_env_file=None,weekly_finik_payment_url='https://qr.finik.kg/weekly-499',lifetime_finik_payment_url='https://qr.finik.kg/forever-999')
    result=ContactResult('unpaid',SimpleNamespace(id=42))
    service=SimpleNamespace(contact_status=AsyncMock(return_value=result),begin_payment=AsyncMock(return_value=SimpleNamespace(request=SimpleNamespace(status='awaiting_receipt',plan=plan))))
    runtime=SimpleNamespace(workflow_data={'service':service,'terms_consents':SimpleNamespace(accept=AsyncMock()),'payments':object()})
    user=SimpleNamespace(id=123,username=None,first_name='Test')
    monkeypatch.setattr(web,'_miniapp_context',lambda payload:(settings,runtime,user,42))
    response=await web.miniapp_start_payment(web.MiniAppRequest(init_data='test',start_param='test',plan=plan),BackgroundTasks())
    assert response['plan']==plan and response['price']==price
    assert response['payment_url']==(settings.lifetime_finik_payment_url if plan=='lifetime' else settings.weekly_finik_payment_url)
    assert service.begin_payment.await_args.kwargs['plan']==plan


@pytest.mark.asyncio
async def test_background_preparation_cannot_replace_selected_lifetime_plan(repositories):
    from tests.helpers import make_ad
    apartments,payments,_=repositories
    apartment=await apartments.upsert_discovered(make_ad())
    await payments.submit(user_id=123,apartment_id=apartment.id,username=None,first_name='Test',plan='lifetime')
    prepared=await payments.submit(user_id=123,apartment_id=apartment.id,username=None,first_name='Test',plan='week',prepare_only=True)
    assert prepared.request.plan=='lifetime' and prepared.request.status=='awaiting_receipt'


@pytest.mark.asyncio
async def test_unoffered_plan_is_rejected_before_payment(monkeypatch):
    service=SimpleNamespace(contact_status=AsyncMock(return_value=ContactResult('unpaid',None)),begin_payment=AsyncMock())
    monkeypatch.setattr(web,'_miniapp_context',lambda payload:(Settings(_env_file=None),SimpleNamespace(workflow_data={'service':service}),SimpleNamespace(id=1),42))
    with pytest.raises(HTTPException) as error:
        await web.miniapp_start_payment(web.MiniAppRequest(init_data='test',start_param='test',plan='month'),BackgroundTasks())
    assert error.value.status_code==400
    service.begin_payment.assert_not_awaited()

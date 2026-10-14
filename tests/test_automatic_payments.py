import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, func

from app import web
from app.config import Settings, get_settings
from app.models import PaymentHistory, PaymentRuntimeConfig
from app.payment_plans import WEEK_PLAN
from app.payment_runtime import read_config, write_config, load_enabled_config
from app.payments.signals import notify, subscribe, unsubscribe, waiters
from app.finik import canonical_request, sign_request
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_verified_payment_grants_once_without_customer_claim(repositories, service):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=3119191))
    submission = await service.begin_payment(user_id=3119191, apartment_id=apartment.id,
        username="automatic", first_name="Paid", plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "verified-paid")
    outcome, _ = await payments.apply_provider_result("verified-paid", succeeded=True, amount=498, automatic_access=True)
    assert outcome == "amount_mismatch"
    assert (await service.contact_status(3119191, apartment.id)).status != "approved"
    before = datetime.now(timezone.utc)
    outcome, request = await payments.apply_provider_result("verified-paid", succeeded=True, amount=499, automatic_access=True)
    assert outcome == "approved" and request.approved_by is None
    assert before + timedelta(days=7) <= request.access_expires_at
    assert (await service.contact_status(3119191, apartment.id)).status == "approved"
    expiry = request.access_expires_at
    outcome, request = await payments.apply_provider_result("verified-paid", succeeded=True, amount=499, automatic_access=True)
    assert outcome == "already_approved" and request.access_expires_at.replace(tzinfo=timezone.utc) == expiry
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(PaymentHistory)) == 1


@pytest.mark.asyncio
async def test_failed_payment_never_grants(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=3119192))
    submission = await service.begin_payment(user_id=3119192, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "failed-paid")
    assert (await payments.apply_provider_result("failed-paid", succeeded=False, amount=499, automatic_access=True))[0] == "failed"
    assert (await service.contact_status(3119192, apartment.id)).status != "approved"


@pytest.mark.asyncio
async def test_runtime_key_is_encrypted_and_only_validated_config_loads(repositories):
    sessions = repositories[2]
    secret = "operator-encryption-secret"
    config = {"api_key":"private-example-acquiring-key", "enabled":False}
    await write_config(sessions, secret, config)
    async with sessions() as s:
        row = await s.get(PaymentRuntimeConfig, "finik_configuration")
        assert config["api_key"] not in row.value
    settings = Settings(_env_file=None, callback_secret=secret)
    await load_enabled_config(settings, sessions)
    assert not settings.finik_automatic_access_enabled
    config["enabled"] = True
    await write_config(sessions, secret, config)
    await load_enabled_config(settings, sessions)
    assert settings.finik_api_key == config["api_key"] and settings.finik_automatic_access_enabled
    assert (await read_config(sessions, secret))["enabled"]


@pytest.mark.asyncio
async def test_payment_signal_wakes_all_screens_and_cleans_up():
    first, second = subscribe(311991), subscribe(311991)
    notify(311991)
    await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), .1)
    unsubscribe(311991, first)
    unsubscribe(311991, second)
    assert 311991 not in waiters


def test_auto_mode_uses_individual_checkout_over_shared_qr():
    settings = Settings(_env_file=None, finik_api_key="configured", finik_account_id="account",
        finik_private_key_pem="configured", telegram_webhook_url="https://example.test/telegram/webhook",
        finik_automatic_access_enabled=True)
    assert web._uses_dynamic_finik(settings, WEEK_PLAN)
    assert web._finik_payment_url(settings, WEEK_PLAN) == ""


@pytest.mark.asyncio
async def test_forged_callback_cannot_issue_access(monkeypatch):
    for name, value in {"RUN_BOT":"true", "FINIK_API_KEY":"configured", "FINIK_ACCOUNT_ID":"configured",
        "FINIK_PRIVATE_KEY_PEM":"configured", "TELEGRAM_WEBHOOK_URL":"https://example.test/telegram/webhook",
        "FINIK_AUTOMATIC_ACCESS_ENABLED":"true"}.items():
        monkeypatch.setenv(name,value)
    get_settings.cache_clear()
    payments = SimpleNamespace(apply_provider_result=AsyncMock())
    monkeypatch.setattr(web,"_bot_runtime",SimpleNamespace(workflow_data={"payments":payments}))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url="http://test") as client:
            response = await client.post("/finik/webhook", headers={"x-api-timestamp":str(int(time.time()*1000))},
                json={"status":"success","amount":499,"fields":{"paymentId":"guessed-id"}})
        assert response.status_code == 401
        payments.apply_provider_result.assert_not_awaited()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_watch_wakes_on_grant_without_database_poll_loop(monkeypatch):
    settings = Settings(_env_file=None, finik_api_key="configured", finik_account_id="account",
        finik_private_key_pem="configured", telegram_webhook_url="https://example.test/telegram/webhook",
        finik_automatic_access_enabled=True)
    apartment = SimpleNamespace(rooms="1",district="ЦУМ",city="Бишкек",price=30000,deposit=None,
        source_description="",photo_urls=[],phone="+996700000001")
    unpaid = SimpleNamespace(status="awaiting_receipt",apartment=apartment,plan=WEEK_PLAN,access_expires_at=None)
    approved = SimpleNamespace(status="approved",apartment=apartment,plan=WEEK_PLAN,access_expires_at=None)
    service = SimpleNamespace(contact_status=AsyncMock(side_effect=[unpaid,approved]))
    monkeypatch.setattr(web,"_miniapp_context",lambda _: (settings,SimpleNamespace(workflow_data={"service":service}),SimpleNamespace(id=31991),1))
    task = asyncio.create_task(web.miniapp_watch_payment(web.MiniAppRequest(init_data="test",start_param="test")))
    await asyncio.sleep(0)
    notify(31991)
    response = await asyncio.wait_for(task, .5)
    assert response["status"] == "approved"
    assert service.contact_status.await_count == 2
    assert 31991 not in waiters


@pytest.mark.asyncio
async def test_signed_webhook_and_tamper_rejection_end_to_end(repositories, service, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    settings = Settings(_env_file=None, run_bot=True, finik_api_key="configured", finik_account_id="account",
        finik_private_key_pem=private, finik_webhook_public_key_pem=public,
        telegram_webhook_url="https://example.test/telegram/webhook",finik_automatic_access_enabled=True)
    monkeypatch.setattr(web,"get_settings",lambda:settings)
    apartments,payments,sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=331991))
    submitted = await service.begin_payment(user_id=331991,apartment_id=apartment.id,username=None,first_name=None,plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submitted.request.id,"signed-payment")
    monkeypatch.setattr(web,"_bot_runtime",SimpleNamespace(workflow_data={"payments":payments}))
    body = {"status":"success","amount":499,"fields":{"paymentId":"signed-payment"}}
    headers = {"x-api-timestamp":str(int(time.time()*1000))}
    headers["signature"] = sign_request(canonical_request(method="POST",path="/finik/webhook",host="test",headers=headers,body=body),private)
    event = subscribe(331991)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url="http://test") as client:
            tampered = await client.post("/finik/webhook",headers=headers,json={**body,"amount":500})
            assert tampered.status_code == 401 and not event.is_set()
            response = await client.post("/finik/webhook",headers=headers,json=body)
            assert response.json() == {"status":"approved"}
            assert event.is_set()
            repeated = await client.post("/finik/webhook",headers=headers,json=body)
            assert repeated.json() == {"status":"already_approved"}
        assert (await service.contact_status(331991,apartment.id)).status == "approved"
        async with sessions() as s:
            assert await s.scalar(select(func.count()).select_from(PaymentHistory)) == 1
    finally:
        unsubscribe(331991,event)

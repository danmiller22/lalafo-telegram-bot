"""Failures found while reviewing checkout, worker recovery and discovery."""
import asyncio
import hashlib
import hmac
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url

from app import web
from app.database import create_engine_and_session, init_db, normalize_database_url
from app.matching.repository import MatchingRepository
from app.models import Apartment, ApartmentMatchDelivery, PaymentRequest
from app.payment_plans import WEEK_PLAN, MONTH_PLAN, LIFETIME_PLAN
from app.payments.repository import ApartmentRepository
from app.payments.review import notify_payment_admin
from app.state import ListingIndex, same_listing
from app.security import TokenSigner, base36_encode
from app.telegram.miniapp import TelegramMiniAppUser, verify_telegram_init_data
from tests.helpers import make_ad
from tests.test_miniapp import signed_init_data
from tests.test_payment_review_notifications import review_setup


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [WEEK_PLAN, MONTH_PLAN, LIFETIME_PLAN])
async def test_all_dynamic_finik_plans_renew_expired_checkout(repositories, service, plan):
    apartments, payments, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=plan)
    request_id = submission.request.id
    await payments.prepare_provider_payment(request_id, "old", configuration_id="merchant")
    await payments.set_provider_payment_url(request_id, "https://qr.finik.kg/old",
        configuration_id="merchant", payment_id="old")
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == request_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    renewed = await payments.prepare_provider_payment(request_id, "new", configuration_id="merchant")
    assert renewed.provider_payment_id == "new"
    assert renewed.provider_payment_url is None
    assert renewed.created_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) - timedelta(seconds=10)
    # A late response for the old checkout cannot overwrite the new link.
    assert not await payments.set_provider_payment_url(request_id, "https://qr.finik.kg/old",
        configuration_id="merchant", payment_id="old")


@pytest.mark.asyncio
async def test_checkout_response_does_not_erase_provider_success(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    request_id = submission.request.id
    await payments.prepare_provider_payment(request_id, "paid", configuration_id="merchant")
    assert (await payments.apply_provider_result("paid", succeeded=True, amount=499))[0] == "awaiting_confirmation"
    assert not await payments.set_provider_payment_url(request_id, "https://qr.finik.kg/late",
        configuration_id="merchant", payment_id="paid")
    preserved = await payments.prepare_provider_payment(request_id, "unused", configuration_id="merchant")
    assert preserved.provider_status == "succeeded"
    assert preserved.provider_payment_id == "paid"
    assert (await service.contact_status(1, apartment.id)).status == "awaiting_receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["bad", {}, [], None, "NaN", "Infinity", 498])
async def test_malformed_provider_amount_is_rejected(repositories, service, amount):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "payment")
    outcome, _ = await payments.apply_provider_result("payment", succeeded=True, amount=amount)
    assert outcome == "amount_mismatch"
    assert (await service.contact_status(1, apartment.id)).status != "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_late_mismatched_callback_preserves_acknowledged_payment(repositories, service, approved):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "payment")
    await payments.apply_provider_result("payment", succeeded=True, amount=499)
    if approved:
        await payments.mark_payment_claimed(user_id=1, apartment_id=apartment.id)
        await payments.decide(submission.request.id, approve=True, admin_id=999)
    outcome, request = await payments.apply_provider_result("payment", succeeded=True, amount=498)
    assert outcome == "amount_mismatch"
    assert request.provider_status == "succeeded"
    assert request.status == ("approved" if approved else "awaiting_receipt")


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", [False, True])
async def test_contacts_and_checkouts_reject_unavailable_listing_before_sweep(repositories, service, inactive):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    values = {"availability_status": "unavailable"} if inactive else {
        "published_at": datetime.now(timezone.utc) - timedelta(days=2), "availability_status": "active"}
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id == apartment.id).values(**values))
    assert (await service.contact_status(1, apartment.id)).status == "unavailable"
    with pytest.raises(LookupError):
        await service.begin_payment(user_id=1, apartment_id=apartment.id,
            username=None, first_name=None, plan=WEEK_PLAN)


@pytest.mark.asyncio
async def test_notification_lease_recovers_after_restart_and_rejects_old_worker(repositories, service):
    _, request, runtime = await review_setup(repositories, service)
    payments, sessions = repositories[1:]
    assert await payments.claim_admin_notification(request.id, token="old-worker")
    assert not await payments.claim_admin_notification(request.id, token="new-worker")
    assert await payments.pending_notifications() == []
    async with sessions.begin() as session:
        await session.execute(update(PaymentRequest).where(PaymentRequest.id == request.id)
            .values(admin_notification_claimed_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
    assert [r.id for r in await payments.pending_notifications()] == [request.id]
    assert await payments.claim_admin_notification(request.id, token="new-worker")
    assert not await payments.finish_admin_notification(request.id, 111, token="old-worker")
    await payments.release_admin_notification(request.id, token="old-worker")
    assert await payments.finish_admin_notification(request.id, 222, token="new-worker")
    assert (await payments.get_request(request.id)).admin_message_id == 222
    assert (await service.contact_status(121, request.apartment_id)).status == "pending"


@pytest.mark.asyncio
async def test_cancelled_notification_can_be_retried(repositories, service):
    _, request, runtime = await review_setup(repositories, service)
    runtime.bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await notify_payment_admin(runtime.bot, repositories[1], runtime.workflow_data["settings"],
            runtime.workflow_data["signer"], request)
    assert (await repositories[1].get_request(request.id)).admin_message_id is None
    runtime.bot.send_message.side_effect = None
    assert await notify_payment_admin(runtime.bot, repositories[1], runtime.workflow_data["settings"],
        runtime.workflow_data["signer"], request)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "inactive", "filters", "notifications"])
async def test_matching_claim_skips_invalid_queued_card_and_finds_next(repositories, change):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=1, rooms=["1"], districts=[], all_districts=True,
        max_budget=40_000, source="test")
    first = await apartments.upsert_discovered(make_ad(lalafo_id=1, price=40_000))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=2, price=30_000))
    for apartment in (first, second):
        await apartments.mark_published(apartment.id, chat_id=-100, message_id=apartment.id)
    assert await matching.seed_notifications() == 2
    if change == "filters":
        await matching.save_profile(user_id=1, rooms=["1"], districts=[], all_districts=True,
            max_budget=30_000, source="test")
    elif change == "notifications":
        await matching.toggle_notifications(1)
    else:
        values = {"active": False} if change == "inactive" else {
            "published_at": datetime.now(timezone.utc) - timedelta(days=2)}
        async with sessions.begin() as session:
            await session.execute(update(Apartment).where(Apartment.id == first.id).values(**values))
    # Control order: seed_notifications scans newest first.
    async with sessions.begin() as session:
        await session.execute(update(ApartmentMatchDelivery).where(ApartmentMatchDelivery.apartment_id == first.id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    claim = await matching.claim_delivery()
    if change == "notifications":
        assert claim is None
    else:
        assert claim is not None and claim.apartment.id == second.id
    async with sessions() as session:
        row = await session.scalar(select(ApartmentMatchDelivery).where(ApartmentMatchDelivery.apartment_id == first.id))
        assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_paid_start_does_not_require_working_checkout_settings(monkeypatch):
    from fastapi import BackgroundTasks
    apartment = SimpleNamespace(phone="+996555123456", rooms="1", district="Центр", city="Бишкек",
        price=30_000, deposit=None, source_description="", photo_urls=[])
    approved = SimpleNamespace(status="approved", apartment=apartment, plan=WEEK_PLAN, access_expires_at=None)
    runtime = SimpleNamespace(workflow_data={"service": SimpleNamespace(contact_status=AsyncMock(return_value=approved))})
    monkeypatch.setattr(web, "_miniapp_context", lambda _: (SimpleNamespace(), runtime,
        TelegramMiniAppUser(1, "User"), 42))
    response = await web.miniapp_start_payment(
        web.MiniAppRequest(init_data="test", start_param="test"),
        BackgroundTasks(),
    )
    assert response["status"] == "approved" and response["phone"]


@pytest.mark.parametrize("raw_user", [[], "invalid", 5, {"id": -1}, {"id": True}, {"id": "1"}])
def test_signed_malformed_telegram_user_is_rejected(raw_user):
    token = "123456:telegram-test-token"
    fields = dict(parse_qsl(signed_init_data(bot_token=token, user_id=1, auth_date=1_700_000_000)))
    fields.pop("hash")
    fields["user"] = json.dumps(raw_user)
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, "\n".join(f"{k}={v}" for k,v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    assert verify_telegram_init_data(urlencode(fields), bot_token=token, now=1_700_000_100) is None


def test_duplicate_index_matches_reference_rules():
    rng = random.Random(42)
    photos = [f"https://img.example/original/photo-{i:012d}.jpg" for i in range(12)]
    def ad(i):
        return make_ad(lalafo_id=i, rooms=rng.choice(["1", "studio"]),
            district=rng.choice([None, "Восток-5", "Восток-5 мкр", "ЦУМ"]),
            price=rng.choice([23_000, 30_000, 32_000, 40_000]),
            phone=rng.choice(["+996555111111", "+996555222222"]),
            photo_urls=rng.sample(photos, rng.randint(0, 4)))
    published = [ad(i) for i in range(100)]
    index = ListingIndex(published)
    for candidate in [*published, *(ad(i) for i in range(100, 350))]:
        assert index.contains(candidate) == any(same_listing(candidate, item) for item in published)


@pytest.mark.parametrize("sslmode", ["require", "verify-full", "disable"])
def test_database_url_normalization_preserves_credentials_and_ssl_mode(sslmode):
    raw = f"postgres://user:sslmode%3Drequire%26channel_binding%3Drequire@db.test/app?channel_binding=require&sslmode={sslmode}"
    parsed = make_url(normalize_database_url(raw))
    assert parsed.password == "sslmode=require&channel_binding=require"
    assert parsed.query == {"ssl": sslmode}


@pytest.mark.asyncio
async def test_collectors_concurrently_upsert_same_source(tmp_path):
    engine, sessions = create_engine_and_session(f"sqlite:///{tmp_path / 'collectors.db'}")
    try:
        await init_db(engine)
        repository = ApartmentRepository(sessions)
        results = await asyncio.gather(*(repository.upsert_discovered(make_ad(), discovery_priority=(i == 0)) for i in range(5)))
        assert len({row.id for row in results}) == 1
        async with sessions() as session:
            assert await session.scalar(select(func.count(Apartment.id))) == 1
        assert (await repository.get(results[0].id)).discovery_priority
    finally:
        await engine.dispose()


@pytest.mark.parametrize("value", [0, 2**31, 2**63, 2**256])
def test_public_apartment_tokens_cannot_overflow_database(value):
    encoded = base36_encode(value)
    assert TokenSigner.decode_public_id(encoded + ".public") is None
    assert TokenSigner.decode_public_start_id(encoded + "-public") is None
    assert TokenSigner.decode_public_id("1.public") == 1
    assert TokenSigner.decode_public_start_id("1-public") == 1


@pytest.mark.asyncio
async def test_failed_callback_does_not_undo_verified_payment(repositories, service):
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    submission = await service.begin_payment(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)
    await payments.prepare_provider_payment(submission.request.id, "payment")
    await payments.apply_provider_result("payment", succeeded=True, amount=499)
    outcome, request = await payments.apply_provider_result("payment", succeeded=False, amount=None)
    assert outcome == "already_succeeded" and request.provider_status == "succeeded"
    assert (await service.contact_status(1, apartment.id)).status != "approved"


@pytest.mark.asyncio
async def test_queue_skips_invalid_cards_using_one_pool(repositories, monkeypatch):
    from app.config import Settings
    from scripts import publish_inventory as publisher
    apartments, _, sessions = repositories
    invalid = await apartments.upsert_discovered(make_ad(lalafo_id=1, price=20_000,
        source_url="https://t.me/apartments/1"))
    valid = await apartments.upsert_discovered(make_ad(lalafo_id=2, price=30_000,
        source_url="https://t.me/apartments/2"))
    inventory = SimpleNamespace(claim_due=AsyncMock(side_effect=[
        SimpleNamespace(id=1, apartment=invalid, window_key="ordinary"), SimpleNamespace(id=2, apartment=valid, window_key="ordinary")]),
        finish_item=AsyncMock(), retry_item=AsyncMock())
    engine = SimpleNamespace(dispose=AsyncMock())
    initialize = AsyncMock()
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    send = SimpleNamespace(publish=AsyncMock(return_value=SimpleNamespace(message_id=99)))
    monkeypatch.setattr(publisher, "get_settings", lambda: Settings(telegram_bot_token="123456:token", callback_secret="c" * 32))
    monkeypatch.setattr(publisher, "create_engine_and_session", lambda _: (engine, sessions))
    monkeypatch.setattr(publisher, "init_db", initialize)
    monkeypatch.setattr(publisher, "InventoryRepository", lambda _: inventory)
    monkeypatch.setattr(publisher, "Bot", lambda **_: bot)
    monkeypatch.setattr(publisher, "TelegramPublisher", lambda *a, **kw: send)
    assert await publisher.run() == 0
    assert inventory.claim_due.await_count == 2
    inventory.finish_item.assert_any_await(1, status="skipped", error="min_price")
    inventory.finish_item.assert_any_await(2, status="published")
    assert (await apartments.get(valid.id)).telegram_message_id == 99
    initialize.assert_awaited_once_with(engine)
    engine.dispose.assert_awaited_once()
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_pool_is_closed_if_initialization_fails(monkeypatch):
    from scripts import publish_inventory as publisher
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(publisher, "create_engine_and_session", lambda _: (engine, None))
    monkeypatch.setattr(publisher, "init_db", AsyncMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        await publisher.run()
    engine.dispose.assert_awaited_once()

@pytest.mark.parametrize('existing_count,candidate_count,price', [(1,3,30000),(3,1,30000),(3,3,30000),(1,3,31000)])
def test_listing_index_preserves_single_cover_duplicate_rule(existing_count,candidate_count,price):
    from tests.helpers import make_ad
    shared='https://img5.lalafo.com/shared-cover-image-123456.jpg'
    existing=make_ad(lalafo_id=1,price=30000,photo_urls=[shared]+[f'https://img5.lalafo.com/existing-extra-image-{i}.jpg' for i in range(existing_count-1)])
    candidate=make_ad(lalafo_id=2,price=price,phone='+996555987654',photo_urls=[shared]+[f'https://img5.lalafo.com/candidate-extra-image-{i}.jpg' for i in range(candidate_count-1)])
    assert ListingIndex([existing]).contains(candidate)==same_listing(candidate,existing)

@pytest.mark.asyncio
async def test_queue_dispatch_reuses_customer_pool_and_claims_empty_queue_once(repositories,monkeypatch):
    from app import web
    from scripts import publish_inventory,publish_if_due
    apartments,_,sessions=repositories
    monkeypatch.setattr(web,'_bot_runtime',SimpleNamespace(workflow_data={'apartments':apartments}))
    publish=AsyncMock(return_value=0)
    stats=AsyncMock(return_value=(0,None))
    monkeypatch.setattr(publish_inventory,'run',publish)
    monkeypatch.setattr(publish_if_due,'publication_window_status',stats)
    monkeypatch.setattr(web,'_inventory_queue_status',AsyncMock(return_value=(0,0,None)))
    assert await web._execute_queue_dispatch()==0
    publish.assert_awaited_once()
    assert publish.await_args.kwargs['sessions'] is sessions
    assert stats.await_count==2
    assert all(call.kwargs['sessions'] is sessions for call in stats.await_args_list)

@pytest.mark.asyncio
async def test_publisher_borrowed_pool_avoids_schema_checks_and_owns_no_engine(repositories,monkeypatch):
    from scripts import publish_inventory as publisher
    _,_,sessions=repositories
    monkeypatch.setattr(publisher,'create_engine_and_session',lambda *args:pytest.fail('must reuse the supplied pool'))
    monkeypatch.setattr(publisher,'init_db',AsyncMock(side_effect=AssertionError('no schema checks on web ticks')))
    monkeypatch.setattr(publisher,'InventoryRepository',lambda *args:SimpleNamespace(claim_due=AsyncMock(return_value=None)))
    assert await publisher.run(sessions=sessions)==0

@pytest.mark.asyncio
async def test_timed_out_source_skips_old_snapshot_and_publishes_next_card(repositories,monkeypatch):
    from app.config import Settings
    from scripts import publish_inventory as publisher
    apartments,_,sessions=repositories
    old=await apartments.upsert_discovered(make_ad(lalafo_id=230001))
    old.last_seen_at=datetime.now(timezone.utc)-timedelta(days=3)
    fresh=await apartments.upsert_discovered(make_ad(lalafo_id=230002,source_url='https://t.me/apartments/230002',phone='+996555987654'))
    class SlowSource:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def detail(self,*args):await asyncio.Event().wait()
    inventory=SimpleNamespace(claim_due=AsyncMock(side_effect=[SimpleNamespace(id=1,apartment=old,window_key='ordinary'),SimpleNamespace(id=2,apartment=fresh,window_key='ordinary')]),finish_item=AsyncMock(),retry_item=AsyncMock())
    bot=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    send=SimpleNamespace(publish=AsyncMock(return_value=SimpleNamespace(message_id=999)))
    monkeypatch.setattr(publisher,'get_settings',lambda:Settings(telegram_bot_token='123456:token',callback_secret='c'*32))
    monkeypatch.setattr(publisher,'PUBLIC_DETAIL_REFRESH_SECONDS',0.01)
    monkeypatch.setattr(publisher,'LalafoClient',lambda **kwargs:SlowSource())
    monkeypatch.setattr(publisher,'InventoryRepository',lambda *args:inventory)
    monkeypatch.setattr(publisher,'Bot',lambda **kwargs:bot)
    monkeypatch.setattr(publisher,'TelegramPublisher',lambda *args,**kwargs:send)
    assert await asyncio.wait_for(publisher.run(sessions=sessions),timeout=1)==0
    inventory.finish_item.assert_any_await(1,status='skipped',error='stale_TimeoutError')
    inventory.finish_item.assert_any_await(2,status='published')
    send.publish.assert_awaited_once()

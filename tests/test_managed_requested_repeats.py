import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.inventory import InventoryRepository, as_utc
from app.managed_cards import PREFIX, ManagedCardUnavailable, refresh_managed_card
from app.models import ApartmentInventoryQueue, LalafoAutoReplyMeta, RequestedCardPromotion
from app.promotions import promotion_work
from scripts.managed_requested_repeats import enroll, reservation_time
from tests.helpers import make_ad

NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
BINDING = {'ad_id': 117338212, 'profile_id': 18194628,
           'url': 'https://lalafo.kg/bishkek/ads/example-id-117338212', 'enabled': True}


@pytest.mark.asyncio
async def test_three_hour_managed_batch_sends_now_then_every_three_hours_until_withdrawn(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1,
                                    acknowledged_at=NOW-timedelta(minutes=30))
    manifest = {'batch': 'three-hour', 'repeat_every_hours': 3, 'repeat_while_active': True,
                'cards': [{**BINDING, 'source_id': 12345}]}
    await enroll(sessions, manifest, now=NOW)
    async def due(when):
        async with sessions() as session:
            rows, _ = await promotion_work(session, now=when,
                day_start=when.replace(hour=0, minute=0, second=0),
                day_end=when.replace(hour=0, minute=0, second=0)+timedelta(days=1))
            return [row[1].id for row in rows]
    assert await due(NOW) == [apartment.id]
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=NOW)
    assert await due(NOW+timedelta(hours=2, minutes=59)) == []
    assert await due(NOW+timedelta(hours=3)) == [apartment.id]
    # Ongoing managed repeats survive the ordinary one-day requested campaign.
    assert await due(NOW+timedelta(days=2)) == [apartment.id]
    await apartments.set_availability(apartment.id, status='unavailable',
        checked_at=NOW+timedelta(days=2), reason='source_removed')
    assert await due(NOW+timedelta(days=2)) == []


def public_ad(**overrides):
    data = {'id': 117338212, 'user_id': 18194628, 'status_id': 2,
            'category_id': 2044, 'city_id': 103184, 'currency': 'KGS', 'price': 32000,
            'description': 'Без мебели. Депозит 10000 сом.',
            'images': [{'original_url': 'https://img/room'}, {'original_url': 'https://img/kitchen'}],
            'params': [{'id': 69, 'values': [{'id': 2773, 'selected': True}]},
                       {'id': 357, 'values': [{'value': 'Филармония', 'selected': True}]},
                       {'id': 947, 'value': '10000'}]}
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_public_refresh_preserves_original_verified_contact():
    client = SimpleNamespace(_get_json=AsyncMock(return_value=public_ad()))
    original = make_ad(phone='')
    result = await refresh_managed_card(client, BINDING, original)
    assert result.phone == original.phone and result.lalafo_id == original.lalafo_id
    assert result.source_url == original.source_url
    assert result.price == 32000 and result.deposit == 10000
    assert result.district == 'Филармония' and result.photo_urls == ['https://img/room', 'https://img/kitchen']
    assert client._get_json.await_count == 1


@pytest.mark.asyncio
async def test_one_day_payment_waiting_repeat_uses_owner_verified_snapshot():
    from app.lalafo.client import LalafoNotFound

    snapshot = {
        'status_id': 11, 'price': 35000, 'rooms': '1',
        'district': 'Восток-5', 'deposit': None,
        'photo_urls': ['https://img/new-room', 'https://img/new-kitchen'],
        'source_description': 'Карточка из кабинета владельца',
    }
    binding = {**BINDING, 'payment_waiting_snapshot': snapshot}
    client = SimpleNamespace(
        _get_json=AsyncMock(side_effect=LalafoNotFound('not public')),
    )
    original = make_ad(phone='+996555123456')
    result = await refresh_managed_card(client, binding, original)
    assert result.phone == original.phone
    assert result.price == 35000 and result.district == 'Восток-5'
    assert result.photo_urls == snapshot['photo_urls']


@pytest.mark.parametrize('changes', [{'status_id': 3}, {'user_id': 2}, {'id': 1},
                                     {'category_id': 1}, {'city_id': 1}])
@pytest.mark.asyncio
async def test_withdrawn_or_changed_card_cannot_repeat(changes):
    client = SimpleNamespace(_get_json=AsyncMock(return_value=public_ad(**changes)))
    with pytest.raises(ManagedCardUnavailable):
        await refresh_managed_card(client, BINDING, make_ad())


@pytest.mark.asyncio
async def test_enrollment_is_idempotent_and_empty_phone_requires_explicit_binding(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(phone=''))
    await apartments.mark_published(apartment.id, chat_id=-1004389602150, message_id=45857,
                                    acknowledged_at=NOW-timedelta(hours=7))
    manifest = {'batch': 'reviewed-test', 'cards': [{**BINDING, 'source_id': 12345}]}
    await enroll(sessions, manifest, now=NOW)
    await enroll(sessions, manifest, now=NOW+timedelta(hours=12))
    async with sessions() as session:
        promotion = await session.get(RequestedCardPromotion, apartment.id)
        assert as_utc(promotion.expires_at) == NOW+timedelta(days=1)
        assert not promotion.enabled  # older workers cannot refresh the wrong original
        due, _ = await promotion_work(session, now=NOW, day_start=NOW-timedelta(hours=9), day_end=NOW+timedelta(hours=12))
        assert [row[1].id for row in due] == [apartment.id]
    async with sessions.begin() as session:
        await session.delete(await session.get(LalafoAutoReplyMeta, PREFIX+str(apartment.id)))
    async with sessions() as session:
        assert await promotion_work(session, now=NOW, day_start=NOW-timedelta(hours=9), day_end=NOW+timedelta(hours=12)) == ([], 0)


@pytest.mark.asyncio
async def test_managed_claim_reserves_shared_slot_and_cannot_claim_ordinary_rows(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=apartment.id, scheduled_at=NOW,
                                            window_key='ordinary', sequence=0, status='queued'))
    repo = InventoryRepository(sessions)
    assert await repo.claim_due(now=NOW, only_apartment_ids={apartment.id}, requested_only=True) is None
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1,
                                    acknowledged_at=NOW-timedelta(hours=7), promote_requested=True)
    item = await repo.claim_due(now=NOW+timedelta(minutes=10), only_apartment_ids={apartment.id},
                               requested_only=True, reservation_clock=NOW)
    assert item is not None and item.window_key.startswith('requested-card:')
    assert as_utc(item.claimed_at) == NOW
    assert await repo.claim_due(now=NOW+timedelta(minutes=11)) is None


def test_reservations_respect_daily_cap_clock_and_minimum_gap():
    assert reservation_time(NOW, 110, None) is None
    assert reservation_time(NOW.replace(hour=21), 0, None) is None  # 03:00 Bishkek
    assert reservation_time(NOW, 0, NOW) == NOW+timedelta(minutes=11)
    assert reservation_time(NOW, 0, NOW-timedelta(minutes=4)) == NOW+timedelta(minutes=7)
    assert reservation_time(NOW, 0, None) == NOW

@pytest.mark.asyncio
async def test_public_feed_compact_params_are_supported():
    data = public_ad(params=[{'id': 69, 'value': '1 комната', 'value_id': 2773},
                             {'id': 357, 'value': 'Филармония'}, {'id': 947, 'value': '10000'}])
    client = SimpleNamespace(_get_json=AsyncMock(return_value=data))
    result = await refresh_managed_card(client, BINDING, make_ad())
    assert result.rooms == '1' and result.deposit == 10000


@pytest.mark.parametrize('public_error', [False, True])
@pytest.mark.asyncio
async def test_managed_repeat_checks_current_ad_before_send_preserves_private_original(repositories, monkeypatch, public_error):
    from app.config import Settings
    from app.lalafo.client import LalafoAccessError
    from app.models import RequestedCardPublication
    from sqlalchemy import select
    from scripts import publish_inventory

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(publish_inventory, 'datetime', Clock)
    apartments, _, sessions = repositories
    original = make_ad(phone='', photo_urls=['https://img/old1', 'https://img/old2'],
                       source_description='Исходное проверенное описание')
    apartment = await apartments.upsert_discovered(original)
    await apartments.mark_published(apartment.id, chat_id=-1004389602150, message_id=45857,
                                    acknowledged_at=NOW-timedelta(hours=7))
    await enroll(sessions, {'batch': 'integration', 'cards': [{**BINDING, 'source_id': 12345}]}, now=NOW)
    inventory = InventoryRepository(sessions)
    item = await inventory.claim_due(now=NOW, only_apartment_ids={apartment.id}, requested_only=True)
    assert item is not None
    json_get = AsyncMock(return_value=public_ad())
    if public_error:
        json_get.side_effect = LalafoAccessError('403', status_code=403)
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        _get_json = json_get
    publish = AsyncMock(return_value=SimpleNamespace(message_id=45999, date=NOW))
    monkeypatch.setattr(publish_inventory, 'LalafoClient', Client)
    monkeypatch.setattr(publish_inventory, 'Bot', lambda **kw: SimpleNamespace(session=SimpleNamespace(close=AsyncMock())))
    monkeypatch.setattr(publish_inventory, 'TelegramPublisher', lambda *a, **kw: SimpleNamespace(publish=publish))
    settings = Settings(_env_file=None, telegram_bot_token='123:fake', callback_secret='test-secret-long-enough',
                        telegram_group_id=-1004389602150)
    result = await publish_inventory._publish_item(
        settings, sessions, inventory, apartments, item
    )
    assert result is (None if public_error else 0)
    stored = await apartments.get(apartment.id)
    assert stored.source_description == original.source_description
    assert stored.photo_urls == original.photo_urls and stored.phone == ''
    if public_error:
        publish.assert_not_awaited()
        assert stored.active  # HTTP 403 is not a confirmed withdrawal
    else:
        publish.assert_awaited_once()
        assert publish.await_args.args[1].price == 32000
        # Managed repeats use the publisher's normal payment/availability buttons.
        assert 'reply_markup' not in publish.await_args.kwargs
        async with sessions() as session:
            receipts = (await session.scalars(select(RequestedCardPublication))).all()
            assert len(receipts) == 2 and receipts[-1].telegram_message_id == 45999
            assert (await session.get(ApartmentInventoryQueue, item.id)).status == 'published'

@pytest.mark.asyncio
async def test_generic_worker_cannot_expose_managed_queued_rows_to_old_web(repositories):
    from sqlalchemy import select
    apartments, _, sessions = repositories
    originals = []
    for index in range(2):
        apartment = await apartments.upsert_discovered(make_ad(lalafo_id=12345+index,
            phone=f'+99655512345{index}', photo_urls=[f'https://img/{index}/a', f'https://img/{index}/b']))
        await apartments.mark_published(apartment.id, chat_id=-1004389602150,
                                        message_id=45857+index, acknowledged_at=NOW-timedelta(hours=7))
        originals.append(apartment)
    await enroll(sessions, {'batch': 'two-managed', 'cards': [
        {**BINDING, 'ad_id': BINDING['ad_id']+index,
         'url': f"https://lalafo.kg/bishkek/ads/example-id-{BINDING['ad_id']+index}",
         'source_id': 12345+index} for index in range(2)]}, now=NOW)
    inventory = InventoryRepository(sessions)
    assert await inventory.claim_due(now=NOW) is None
    async with sessions() as session:
        assert not list(await session.scalars(select(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.status == 'queued')))
    claimed = await inventory.claim_due(now=NOW, requested_only=True, only_apartment_ids={originals[0].id})
    assert claimed is not None
    async with sessions() as session:
        rows = list(await session.scalars(select(ApartmentInventoryQueue)))
        assert len(rows) == 1 and rows[0].status == 'publishing'


@pytest.mark.parametrize('response', [{}, {'code': 451, 'message': 'not available'},
                                     public_ad(status_id=None), public_ad(images=[])])
@pytest.mark.asyncio
async def test_incomplete_or_error_payload_is_unknown_not_withdrawn(response):
    client = SimpleNamespace(_get_json=AsyncMock(return_value=response))
    with pytest.raises(ValueError) as error:
        await refresh_managed_card(client, BINDING, make_ad())
    assert not isinstance(error.value, ManagedCardUnavailable)


@pytest.mark.asyncio
async def test_public_room_id_works_independently_of_translation():
    client = SimpleNamespace(_get_json=AsyncMock(return_value=public_ad(
        params=[{'id': 69, 'value': '1 room', 'value_id': 2773}])))
    assert (await refresh_managed_card(client, BINDING, make_ad())).rooms == '1'


def test_queue_catchup_uses_five_minutes_only_when_three_slots_behind():
    from scripts.managed_requested_repeats import reservation_gap
    from app.inventory import publication_slots_due
    due = publication_slots_due(NOW)
    assert reservation_gap(NOW, due-3, catch_up=True) == 5
    assert reservation_gap(NOW, due-2, catch_up=True) == 11
    assert reservation_gap(NOW, due-3, catch_up=False) == 11
    assert reservation_time(NOW, due-3, NOW, catch_up=True) == NOW+timedelta(minutes=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_skip", [False, True])
async def test_general_queue_worker_sends_fresh_stock_without_any_managed_campaign(repositories, monkeypatch, terminal_skip):
    from scripts import managed_requested_repeats as worker
    from app.config import Settings
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(worker, 'datetime', Clock)
    apartments, _, sessions = repositories
    original = await apartments.upsert_discovered(make_ad(owner_listing=False, district="ЦУМ",
        photo_urls=['https://img/room','https://img/kitchen']))
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(worker, 'create_engine_and_session', lambda _: (engine, sessions))
    monkeypatch.setattr(worker, 'get_settings', lambda: Settings(_env_file=None))
    monkeypatch.setenv('QUEUE_RESERVATION_ALL_CARDS', 'true')
    monkeypatch.delenv('MANAGED_REPEAT_MANIFEST', raising=False)
    async def send(settings, factory, inventory, repo, item):
        assert item.apartment_id == original.id
        if terminal_skip:
            await inventory.finish_item(item.id, status='skipped', error='not_found')
            return None
        await inventory.finish_item(item.id, status='published')
        await repo.mark_published(item.apartment_id, chat_id=1, message_id=46100, acknowledged_at=NOW)
        return 0
    delivery = AsyncMock(side_effect=send)
    monkeypatch.setattr(worker, '_publish_item', delivery)
    next_delivery = AsyncMock(return_value=0)
    monkeypatch.setattr(worker, 'publish_next_due', next_delivery)
    assert await worker.run() == 0
    delivery.assert_awaited_once()
    if terminal_skip:
        next_delivery.assert_awaited_once_with(sessions=sessions)
        assert (await apartments.get(original.id)).telegram_message_id is None
    else:
        next_delivery.assert_not_awaited()
        assert (await apartments.get(original.id)).telegram_message_id == 46100
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh_stock,ordinary_sends,expect_managed',
                         [(True,0,False),(True,1,False),(True,2,True),(False,0,True)])
async def test_cloud_publisher_interleaves_managed_repeats_with_fresh_options(
        repositories, monkeypatch, fresh_stock, ordinary_sends, expect_managed):
    from scripts import managed_requested_repeats as worker
    from app.config import Settings
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    apartments,_,sessions=repositories
    managed=await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(managed.id,chat_id=-1004389602150,message_id=1,
                                    acknowledged_at=NOW-timedelta(hours=7))
    await enroll(sessions,{'batch':'interleaving','cards':[{**BINDING,'source_id':12345}]},
                 now=NOW-timedelta(hours=7))
    for index in range(ordinary_sends):
        prior=await apartments.upsert_discovered(make_ad(lalafo_id=20000+index,
            phone=f'+99655520000{index}',photo_urls=[f'https://img/prior{index}']))
        await apartments.mark_published(prior.id,chat_id=1,message_id=10+index,
                                        acknowledged_at=NOW-timedelta(hours=3-index))
    if fresh_stock:
        fresh=await apartments.upsert_discovered(make_ad(lalafo_id=30000,owner_listing=False,
            district="ЦУМ",
            phone='+996555300000',price=28000,photo_urls=['https://img/new1','https://img/new2']))
    monkeypatch.setattr(worker,'datetime',Clock)
    monkeypatch.setattr(worker,'create_engine_and_session',lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    monkeypatch.setattr(worker,'get_settings',lambda:Settings(_env_file=None))
    monkeypatch.setenv('QUEUE_RESERVATION_ALL_CARDS','true')
    monkeypatch.delenv('MANAGED_REPEAT_MANIFEST',raising=False)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(worker,'LalafoClient',Client)
    monkeypatch.setattr(worker,'refresh_managed_card',AsyncMock())
    async def deliver(settings,factory,inventory,repo,item):
        assert item.apartment_id == (managed.id if expect_managed else fresh.id)
        await inventory.finish_item(item.id,status='published')
        await repo.mark_published(item.apartment_id,chat_id=1,message_id=99,acknowledged_at=NOW)
        return 0
    delivery=AsyncMock(side_effect=deliver)
    monkeypatch.setattr(worker,'_publish_item',delivery)
    assert await worker.run()==0
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_future_slot_uses_queue_ack_clock_to_avoid_rounding_rejection(repositories):
    from scripts.managed_requested_repeats import latest_delivery_time
    apartments,_,sessions=repositories
    previous=await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(previous.id,chat_id=1,message_id=1,acknowledged_at=NOW)
    fresh=await apartments.upsert_discovered(make_ad(lalafo_id=40000,phone='+996555400000',
        photo_urls=['https://img/fresh-clock']))
    ack=NOW+timedelta(seconds=2,microseconds=100)
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=previous.id,scheduled_at=NOW,
            window_key='prior',sequence=1,status='published',published_at=ack))
        session.add(ApartmentInventoryQueue(apartment_id=fresh.id,scheduled_at=NOW,
            window_key='next',sequence=2,status='queued'))
    async with sessions() as session:
        latest=await latest_delivery_time(session,[SimpleNamespace(published_at=NOW)])
    assert latest==ack
    when=reservation_time(NOW+timedelta(minutes=1),1,latest,catch_up=True)
    assert when==ack+timedelta(minutes=5)
    assert await InventoryRepository(sessions).claim_due(now=when,reservation_clock=NOW) is not None


@pytest.mark.asyncio
async def test_managed_only_dispatch_uses_shared_backlog_gap(repositories,monkeypatch):
    from scripts import managed_requested_repeats as worker
    from app.config import Settings
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    apartments,_,sessions=repositories
    managed=await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(managed.id,chat_id=1,message_id=1,acknowledged_at=NOW-timedelta(hours=7))
    await enroll(sessions,{'batch':'managed-backlog','cards':[{**BINDING,'source_id':12345}]},now=NOW-timedelta(hours=7))
    previous=await apartments.upsert_discovered(make_ad(lalafo_id=50000,phone='+996555500000',photo_urls=['https://img/previous']))
    await apartments.mark_published(previous.id,chat_id=1,message_id=2,acknowledged_at=NOW-timedelta(minutes=6))
    monkeypatch.setattr(worker,'datetime',Clock)
    monkeypatch.setattr(worker,'create_engine_and_session',lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    monkeypatch.setattr(worker,'get_settings',lambda:Settings(_env_file=None))
    monkeypatch.setenv('QUEUE_RESERVATION_ALL_CARDS','false')
    monkeypatch.delenv('MANAGED_REPEAT_MANIFEST',raising=False)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(worker,'LalafoClient',Client)
    monkeypatch.setattr(worker,'refresh_managed_card',AsyncMock())
    monkeypatch.setattr(worker.asyncio,'sleep',AsyncMock(side_effect=AssertionError('Unneeded delay instead of shared catch-up')))
    async def deliver(settings,factory,inventory,repo,item):
        assert item.apartment_id==managed.id
        await inventory.finish_item(item.id,status='published')
        await repo.mark_published(item.apartment_id,chat_id=1,message_id=3,acknowledged_at=NOW)
        return 0
    delivery=AsyncMock(side_effect=deliver)
    monkeypatch.setattr(worker,'_publish_item',delivery)
    assert await worker.run()==0
    delivery.assert_awaited_once()

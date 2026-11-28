from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import select, update
from app.config import Settings
from app.inventory import REPOST_AFTER_HOURS, MAX_PUBLICATIONS_PER_DAY, publication_window_open
from app.models import Apartment, RentalFeedState
from app.rental_market import daily_medians, market_key, value_rank
from app.rental_feed import claim_cycle, finish_cycle
from app.profile_originals import same_apartment, reserve_original
from scripts.publish_inventory import _valid
from tests.helpers import make_ad


def test_strict_feed_policy():
    assert REPOST_AFTER_HOURS == 72
    assert MAX_PUBLICATIONS_PER_DAY == 100
    settings=Settings()
    assert _valid(make_ad(),settings)[0]
    for changes in [dict(lalafo_id=115809037),dict(source_title='тел 0 (700) 014-496'),
                    dict(source_description='Salut.kg'),dict(price=45001),dict(price=24999),
                    dict(rooms='2'),dict(city='Ош'),dict(no_subletting=False)]:
        assert not _valid(make_ad(**changes),settings)[0]


@pytest.mark.asyncio
async def test_daily_medians_snapshot_and_exclusions(repositories):
    apartments,_,sessions=repositories
    now=datetime.now(timezone.utc)
    for index,price in enumerate([25000,28000,30000,32000,35000]):
        await apartments.upsert_discovered(make_ad(lalafo_id=100+index,price=price,
            phone=f'+99655500010{index}',photo_urls=[f'https://example.com/photo{index}.jpg']))
    await apartments.upsert_discovered(make_ad(lalafo_id=999,price=45000,phone='0700 014 496'))
    async with sessions.begin() as session:
        medians=await daily_medians(session,now=now)
        assert medians[market_key('1','7 мкр')]==30000
        assert value_rank(33000,'1','7 мкр',medians)==0
        assert value_rank(33001,'1','7 мкр',medians)==2
        assert value_rank(44000,'1','Неизвестный район',medians)==1
    await apartments.upsert_discovered(make_ad(lalafo_id=110,price=45000,phone='+996555000110',photo_urls=['https://example.com/extra.jpg']))
    async with sessions.begin() as session:
        assert await daily_medians(session,now=now)==medians
    async with sessions.begin() as session:
        next_day=await daily_medians(session,now=now+timedelta(days=1))
        assert next_day[market_key('1','7 мкр')]==31000


@pytest.mark.asyncio
async def test_cycle_schedule_survives_restart(repositories):
    _,_,sessions=repositories
    assert await claim_cycle(sessions,'test_cycle',interval=timedelta(hours=2))
    assert not await claim_cycle(sessions,'test_cycle',interval=timedelta(hours=2))
    await finish_cycle(sessions,'test_cycle',{'originals_found':0})
    assert not await claim_cycle(sessions,'test_cycle',interval=timedelta(hours=2))


def test_original_match_needs_multiple_identical_photos_and_terms():
    own=make_ad(photo_urls=['https://img/a.jpg','https://img/b.jpg'])
    assert same_apartment(own,make_ad(photo_urls=['https://other/a.jpg','https://other/b.jpg']))
    assert not same_apartment(own,make_ad(photo_urls=['https://img/a.jpg']))
    assert not same_apartment(own,make_ad(photo_urls=own.photo_urls,price=45000))
    assert not same_apartment(own,make_ad(photo_urls=own.photo_urls,district='Джал'))


@pytest.mark.asyncio
async def test_original_send_is_reserved_before_network_and_not_retried_blindly(repositories):
    apartments,_,sessions=repositories
    apartment=await apartments.upsert_discovered(make_ad())
    queue_id=await reserve_original(sessions,apartment)
    assert queue_id is not None
    assert await reserve_original(sessions,apartment) is None
    from app.models import ApartmentInventoryQueue
    async with sessions() as session:
        row=await session.get(ApartmentInventoryQueue,queue_id)
        assert row.status=='uncertain'  # restart cannot turn it into a fresh send


@pytest.mark.asyncio
async def test_empty_active_profile_is_reported_without_fabricating_originals(repositories,monkeypatch):
    from app.profile_originals import scan_profile
    apartments,_,sessions=repositories
    class Feed:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args):return {'items':[]}
    monkeypatch.setattr('app.profile_originals.PublicFeed',Feed)
    runtime=SimpleNamespace(workflow_data={'settings':Settings(),'apartments':apartments})
    await scan_profile(runtime)
    async with sessions() as session:
        row=await session.get(RentalFeedState,'profile_originals')
        assert row.value['active_ads']==0
        assert row.value['published']==0
        assert row.value['originals_found']==0
        assert row.value['last_success_at']


@pytest.mark.asyncio
async def test_ingest_rechecks_bans_without_storing(monkeypatch):
    import app.web as web
    stored=[]
    class Apartments:
        sessions=None
        async def upsert_discovered(self,ad): stored.append(ad)
    async def schedule(*args,**kwargs):return 0
    monkeypatch.setattr(web,'get_settings',lambda:Settings(lalafo_relay_secret='test-secret'))
    monkeypatch.setattr(web,'_bot_runtime',SimpleNamespace(workflow_data={'apartments':Apartments()}))
    monkeypatch.setattr('app.inventory.InventoryRepository.schedule_period',schedule)
    good=make_ad()
    bad=[make_ad(source_title='0703-108-500'),make_ad(lalafo_id=97832535),make_ad(source_description='Салют'),make_ad(price=45001),make_ad(source_url='https://t.me/example/1')]
    response=await web.relay_lalafo_ingest(web.LalafoIngestRequest(ads=[good]+bad),relay_secret='test-secret')
    data=json.loads(response.body)
    assert data['stored']==1 and sum(data['rejected'].values())==5
    assert stored==[good]

@pytest.mark.asyncio
async def test_access_block_is_not_retried_or_rotated():
    import httpx
    from app.lalafo.public_feed import PublicFeed
    requests=[]
    async def blocked(request):
        requests.append(request)
        return httpx.Response(403,json={'error':'access denied'})
    async with PublicFeed(proxy_urls='http://example.invalid:8080') as feed:
        await feed.client.aclose()
        feed.client=httpx.AsyncClient(transport=httpx.MockTransport(blocked))
        with pytest.raises(httpx.HTTPStatusError):
            await feed.get('/api/search/v3/feed/search')
        assert len(requests)==1
        assert feed.route_index==0


def test_one_hundred_slots_fit_the_window_and_regular_interval():
    from app.inventory import BISHKEK, PUBLICATION_SPACING_MINUTES,randomized_period_times,publication_window_open
    import random
    start=datetime(2026,11,28,5,tzinfo=BISHKEK)
    slots=randomized_period_times(start,count=100,rng=random.Random(1))
    assert 10<=PUBLICATION_SPACING_MINUTES<=15
    assert all(publication_window_open(at) for at in slots)
    assert all((b-a).total_seconds()==12.6*60 for a,b in zip(slots,slots[1:]))

@pytest.mark.asyncio
async def test_hosted_feed_fills_queue_without_external_ingest(repositories,monkeypatch):
    from app.rental_feed import refill
    apartments,_,sessions=repositories
    class Feed:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def search(self,*args):
            return {'items':[{'id':100000+i,'status_id':2,'price':30000} for i in range(60)]}
    monkeypatch.setattr('app.rental_feed.PublicFeed',Feed)
    monkeypatch.setattr('app.rental_feed.parsed_public_ad',lambda raw:make_ad(lalafo_id=raw['id'],
        price=30000,phone=f"+996555{raw['id']}",photo_urls=[f"https://example.com/{raw['id']}.jpg"]))
    runtime=SimpleNamespace(workflow_data={'settings':Settings(),'apartments':apartments})
    await refill(runtime)
    from app.models import ApartmentInventoryQueue
    from sqlalchemy import func
    async with sessions() as session:
        state=await session.get(RentalFeedState,'primary_feed')
        assert state.value['state']=='waiting',state.value
        assert state.value['stored']==60
        assert await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='queued'))>=20


@pytest.mark.asyncio
async def test_profile_reuses_mapping_and_records_acknowledged_original(repositories,monkeypatch):
    from app.profile_originals import scan_profile
    apartments,_,sessions=repositories
    photos=['https://img.example/real-a.jpg','https://img.example/real-b.jpg']
    original=make_ad(lalafo_id=100,photo_urls=photos)
    own=make_ad(lalafo_id=200,phone='',photo_urls=photos)
    apartment=await apartments.upsert_discovered(original)
    await apartments.save_managed_lalafo_source(managed_lalafo_ad_id=200,
        managed_lalafo_ad_url='https://lalafo.kg/bishkek/ads/owned-id-200',apartment=apartment)
    class Feed:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args):return {'items':[{'id':200,'user_id':18041923,'status_id':2,'category_id':2044}]}
        async def detail(self,source_id):
            assert source_id in {100,200}
            return {'id':source_id,'user_id':18041923 if source_id==200 else 999}
    class Publisher:
        def __init__(self,*args,**kwargs):pass
        async def publish(self,apartment_id,ad):
            assert ad==original
            return SimpleNamespace(message_id=456,date=datetime.now(timezone.utc))
    monkeypatch.setattr('app.profile_originals.PublicFeed',Feed)
    monkeypatch.setattr('app.profile_originals.TelegramPublisher',Publisher)
    monkeypatch.setattr('app.profile_originals.parsed_public_ad',lambda raw,**kwargs:own if raw['id']==200 else original)
    runtime=SimpleNamespace(bot=object(),workflow_data={'settings':Settings(callback_secret='test-secret-at-least-16',telegram_group_id=-1001),'apartments':apartments})
    await scan_profile(runtime)
    async with sessions() as session:
        row=await session.get(RentalFeedState,'profile_originals')
        assert row.value['originals_found']==1
        assert row.value['published']==1
        receipt=await session.get(RentalFeedState,'original_send:'+str(apartment.id))
        assert receipt.value['state']=='published'
        assert receipt.value['message_id']==456
    assert await reserve_original(sessions,await apartments.get(apartment.id)) is None

@pytest.mark.asyncio
async def test_network_preflight_is_shared_between_hosted_workers(monkeypatch):
    from unittest.mock import AsyncMock
    from app.lalafo.public_feed import PublicFeed
    probe=AsyncMock(return_value=['http://verified.example:8080'])
    monkeypatch.setattr('scripts.select_lalafo_proxy.find_working_proxies',probe)
    monkeypatch.setattr(PublicFeed,'_route_cache',{})
    for _ in range(2):
        async with PublicFeed(proxy_urls='http://configured.example:8080',verify_routes=True) as feed:
            assert feed.routes==['http://verified.example:8080']
    probe.assert_awaited_once_with('http://configured.example:8080')


def test_incremental_listing_index_keeps_duplicate_protection():
    from app.state import ListingIndex
    original=make_ad(lalafo_id=10)
    index=ListingIndex([])
    assert not index.contains(original)
    index.add(original)
    assert index.contains(original)
    assert index.contains(make_ad(lalafo_id=11))
    assert not index.contains(make_ad(lalafo_id=12,phone='+996555999123',photo_urls=['https://example.com/distinct.jpg']))


def test_hidden_phone_profile_terms_never_invent_a_contact():
    from app.lalafo.parser import parse_detail_data,LalafoParseError
    raw={'id':116823000,'hide_phone':True,'category_id':2044,'price':36000,
         'city':'Бишкек','currency':'KGS','params':[{'name':'Количество комнат','value':'1 комната'}]}
    with pytest.raises(LalafoParseError):
        parse_detail_data(raw,source_url='https://lalafo.kg/bishkek/ads/owned-id-116823000')
    own=parse_detail_data(raw,source_url='https://lalafo.kg/bishkek/ads/owned-id-116823000',include_contact=False)
    assert own.phone==''
    assert own.rooms=='1'
    assert not _valid(own,Settings())[0]

@pytest.mark.asyncio
async def test_dispatch_uses_fresh_verified_json_snapshot_without_second_source_read(repositories,monkeypatch):
    from unittest.mock import AsyncMock
    from scripts import publish_inventory as worker
    apartments,_,sessions=repositories
    apartment=await apartments.upsert_discovered(make_ad())
    await apartments.set_availability(apartment.id,status='active',checked_at=datetime.now(timezone.utc),reason='public_feed_status_2')
    apartment=await apartments.get(apartment.id)
    item=SimpleNamespace(id=987,apartment=apartment,window_key='ordinary')
    queue=SimpleNamespace(finish_item=AsyncMock(),retry_item=AsyncMock())
    bot=SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    publisher=SimpleNamespace(publish=AsyncMock(return_value=SimpleNamespace(message_id=900,date=datetime.now(timezone.utc))))
    monkeypatch.setattr(worker,'Bot',lambda **kwargs:bot)
    monkeypatch.setattr(worker,'TelegramPublisher',lambda *args,**kwargs:publisher)
    monkeypatch.setattr('app.lalafo.public_feed.PublicFeed',lambda **kwargs:pytest.fail('Fresh JSON evidence must avoid another source request'))
    settings=Settings(telegram_bot_token='123456:'+('a'*35),callback_secret='test-callback-secret-16',telegram_group_id=-1001)
    assert await worker._publish_item(settings,sessions,queue,apartments,item)==0
    publisher.publish.assert_awaited_once()
    assert publisher.publish.await_args.kwargs=={}
    queue.finish_item.assert_awaited_once_with(987,status='published')

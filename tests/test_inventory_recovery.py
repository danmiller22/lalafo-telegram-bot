from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.availability import AvailabilityService
from app.config import Settings
from app.inventory import InventoryRepository
from app.models import Apartment
from app.payments.repository import ApartmentRepository
from app.lalafo.models import SearchAd, SearchPage
from scripts import scrape_publish
from tests.helpers import make_ad


@pytest.mark.asyncio
@pytest.mark.usefixtures("publication_clock")
async def test_age_expired_listing_needs_revalidation_before_repost(repositories, monkeypatch):
    monkeypatch.setattr("app.availability.datetime", datetime)
    apartments, _, sessions = repositories
    ad = make_ad(lalafo_id=414911, price=30000, district="ЦУМ")
    apartment = await apartments.upsert_discovered(ad)
    await apartments.mark_published(apartment.id,chat_id=-1001,message_id=101)
    now = datetime.now(timezone.utc)
    old = now-timedelta(hours=49)
    async with sessions.begin() as s:
        await s.execute(update(Apartment).where(Apartment.id==apartment.id).values(published_at=old,last_seen_at=old))
    assert await apartments.expire_published_before(cutoff=now-timedelta(hours=48),checked_at=now) == 1
    assert not (await apartments.get(apartment.id)).active
    assert await InventoryRepository(sessions).schedule_period(now=now) == 0
    assert await apartments.repostable_lalafo_ids([ad.lalafo_id],after_hours=48) == {ad.lalafo_id}
    # A successful source refresh revives stock; it does not revive the old card.
    refreshed = await apartments.upsert_discovered(ad)
    assert refreshed.active and refreshed.availability_status == "active"
    assert await InventoryRepository(sessions).schedule_period(now=now) == 1
    assert await apartments.expire_published_before(cutoff=now-timedelta(hours=48),checked_at=now) == 0
    result = await AvailabilityService(apartments,Settings()).check(apartment.id,force=True)
    assert result.status == "unavailable"  # the old Telegram card still expired
    assert (await apartments.get(apartment.id)).active  # keep the validated repeat queued
    claimed = await InventoryRepository(sessions).claim_due(now=now+timedelta(minutes=1))
    assert claimed is not None and claimed.apartment_id == apartment.id


@pytest.mark.asyncio
async def test_confirmed_removal_is_not_an_eligible_repeat(repositories):
    apartments,_,sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=414912))
    await apartments.mark_published(apartment.id,chat_id=-1001,message_id=102)
    now = datetime.now(timezone.utc)
    async with sessions.begin() as s:
        await s.execute(update(Apartment).where(Apartment.id==apartment.id).values(published_at=now-timedelta(hours=49)))
    await apartments.set_availability(apartment.id,status="unavailable",checked_at=now,reason="source_removed")
    assert await apartments.repostable_lalafo_ids([apartment.lalafo_id],after_hours=48) == set()


@pytest.mark.asyncio
async def test_catalogue_refreshes_old_cards_skips_recent_and_batches_queue(repositories,monkeypatch,tmp_path):
    apartments,_,sessions = repositories
    old = make_ad(lalafo_id=414921,source_url="https://lalafo.kg/bishkek/ads/unit-id-414921",price=30000,district="ЦУМ",phone="+996700414921",photo_urls=["https://img/old1","https://img/old2"])
    recent = make_ad(lalafo_id=414922,source_url="https://lalafo.kg/bishkek/ads/unit-id-414922",price=31000,district="Филармония",phone="+996700414922",photo_urls=["https://img/new1","https://img/new2"])
    fresh = make_ad(lalafo_id=414923,source_url="https://lalafo.kg/bishkek/ads/unit-id-414923",price=32000,district="Восток-5",phone="+996700414923",photo_urls=["https://img/fresh1","https://img/fresh2"])
    first = await apartments.upsert_discovered(old)
    second = await apartments.upsert_discovered(recent)
    await apartments.mark_published(first.id,chat_id=-1001,message_id=111)
    await apartments.mark_published(second.id,chat_id=-1001,message_id=112)
    async with sessions.begin() as s:
        await s.execute(update(Apartment).where(Apartment.id==first.id).values(published_at=datetime.now(timezone.utc)-timedelta(hours=49)))
    settings = Settings(_env_file=None, database_url="postgresql://placeholder/db",posted_state_path=tmp_path/"posted.json")
    monkeypatch.setattr(scrape_publish,"get_settings",lambda:settings)
    monkeypatch.setattr("app.database.create_engine_and_session",lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    monkeypatch.setattr("app.database.init_db",AsyncMock())
    for name in ("PRIORITY_AD_SPECS","CURATED_ROTATION_SPECS","CURATED_ROTATION_LALAFO_IDS"):
        monkeypatch.setattr(scrape_publish,name,())
    monkeypatch.setattr(scrape_publish,"INVENTORY_SEARCH_URLS",("https://lalafo.kg/bishkek/owner?price[from]=25000",))
    calls=[]
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["timeout"]<=8
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def search(self,*args,**kwargs):
            return SearchPage(items=[SearchAd(lalafo_id=ad.lalafo_id,detail_url=ad.source_url,price=ad.price,photo_urls=ad.photo_urls) for ad in [old,recent,fresh]],total=3)
        async def detail(self,url):
            ad=next(ad for ad in [old,recent,fresh] if ad.source_url==url)
            calls.append(ad.lalafo_id)
            return ad
    monkeypatch.setattr(scrape_publish,"LalafoClient",Client)
    schedule=AsyncMock(return_value=2)
    monkeypatch.setattr(InventoryRepository,"schedule_period",schedule)
    assert await scrape_publish.run(discovery_only=True,include_telegram_sources=False,collection_budget_seconds=180)==0
    assert set(calls)=={old.lalafo_id,fresh.lalafo_id}
    assert recent.lalafo_id not in calls
    assert schedule.await_count==2  # catalogue batch and final merge, not every card
    assert (await apartments.get_by_lalafo(fresh.lalafo_id)).publication_status=="discovered"


@pytest.mark.asyncio
async def test_exhausted_owner_search_still_stores_and_queues_realtor_fallback(repositories, monkeypatch, tmp_path):
    apartments, _, sessions = repositories
    ad = make_ad(lalafo_id=414930, price=32_000, district="ЦУМ", owner_listing=False)
    settings = Settings(_env_file=None, database_url="postgresql://placeholder/db", posted_state_path=tmp_path/"posted.json")
    monkeypatch.setattr(scrape_publish, "get_settings", lambda: settings)
    monkeypatch.setattr("app.database.create_engine_and_session", lambda _: (SimpleNamespace(dispose=AsyncMock()), sessions))
    monkeypatch.setattr("app.database.init_db", AsyncMock())
    for name in ("PRIORITY_AD_SPECS", "CURATED_ROTATION_SPECS", "CURATED_ROTATION_LALAFO_IDS"):
        monkeypatch.setattr(scrape_publish, name, ())
    monkeypatch.setattr(scrape_publish, "OWNER_SHORTAGE_SEARCH_SECONDS", 0)
    monkeypatch.setattr(scrape_publish, "INVENTORY_SEARCH_URLS", (
        "https://lalafo.kg/bishkek/owner?price[from]=25000",
        "https://lalafo.kg/bishkek/agent/real-estate-agency?price[from]=25000",
    ))
    queries=[]
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def search(self, url, **kwargs):
            queries.append(url)
            assert "real-estate-agency" in url
            return SearchPage(items=[SearchAd(lalafo_id=ad.lalafo_id, detail_url=ad.source_url, price=ad.price)], total=1)
        async def detail(self, url): return ad
    monkeypatch.setattr(scrape_publish, "LalafoClient", Client)
    stats={}
    assert await scrape_publish.run(discovery_only=True, include_telegram_sources=False,
        collection_budget_seconds=10, discovery_stats=stats) == 0
    assert queries and stats["realtor_fallback_started"]
    assert stats["realtor_candidates"] == 1 and stats["realtor_search_pages"] == 1
    assert (await apartments.get_by_lalafo(ad.lalafo_id)).seller_type == "realtor"
    async with sessions() as s:
        from app.models import ApartmentInventoryQueue
        from sqlalchemy import select
        assert (await s.scalar(select(ApartmentInventoryQueue))) is not None

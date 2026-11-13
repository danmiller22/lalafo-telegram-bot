from datetime import datetime,timedelta,timezone
import random
import pytest
from sqlalchemy import select
from app.inventory import plan_period,InventoryRepository,publication_day_start
from app.models import ApartmentInventoryQueue
from tests.test_inventory import _apartments
from tests.helpers import make_ad


def test_plan_uses_remaining_good_areas_before_unknown_reserve():
    center=_apartments(80,central=True,start_id=2000000)
    reserve=_apartments(80,central=False,start_id=2100000)
    for a in center:a.price=35_000
    for a in reserve:a.price=25_000;a.district='Арча-Бешик'
    rows=plan_period(center+reserve,period_start=datetime(2026,11,13,5,tzinfo=timezone(timedelta(hours=6))),rng=random.Random(3))
    assert len(rows)==30
    assert all(row.apartment.district=='Золотой квадрат' for row in rows)


def test_other_areas_remain_eligible_when_priority_stock_is_short():
    reserve=_apartments(80,central=False,start_id=2200000)
    for a in reserve:a.district='Арча-Бешик'
    rows=plan_period(reserve,period_start=datetime(2026,11,13,5,tzinfo=timezone(timedelta(hours=6))),rng=random.Random(4))
    assert len(rows)==30


@pytest.mark.asyncio
async def test_area_quality_wins_before_optional_realtor_allowance(repositories):
    apartments,_,sessions=repositories
    now=publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc)+timedelta(hours=5)
    for i in range(6):
        a=await apartments.upsert_discovered(make_ad(lalafo_id=9900000+i))
        await apartments.mark_published(a.id,chat_id=-1001,message_id=9900000+i,acknowledged_at=now-timedelta(minutes=40+i))
    better=await apartments.upsert_discovered(make_ad(lalafo_id=9910000,district='ЦУМ',price=35_000))
    reserve=await apartments.upsert_discovered(make_ad(lalafo_id=9910001,district='Арча-Бешик',price=25_000,seller_type='realtor',owner_listing=False))
    async with sessions.begin() as session:
        for i,a in enumerate((better,reserve)):
            session.add(ApartmentInventoryQueue(apartment_id=a.id,window_key='quality',sequence=i+1,scheduled_at=now-timedelta(minutes=1)))
    claimed=await InventoryRepository(sessions).claim_due(now=now)
    assert claimed is not None and claimed.apartment_id==better.id

from datetime import datetime, timedelta, timezone
import random

import pytest

from app.inventory import BISHKEK, InventoryRepository, plan_period, publication_day_start
from app.models import ApartmentInventoryQueue
from app.seller_mix import realtor_allowance
from tests.helpers import make_ad
from tests.test_inventory import _apartments


@pytest.mark.parametrize('ordinary,expected', [(120,9),(51,9),(50,10),(49,11),(48,12),(47,11),(10,2),(0,0)])
def test_daily_agent_allowance_tracks_fill_without_breaking_twenty_percent(ordinary, expected):
    assert realtor_allowance(ordinary,60) == expected


@pytest.mark.parametrize('ordinary,total,agents', [(120,60,9),(51,60,9),(50,60,10),(49,60,11),(48,60,12),(47,58,11),(10,12,2),(0,0,0)])
def test_two_periods_publish_sixty_with_adaptive_mix(ordinary,total,agents):
    stock = _apartments(ordinary,central=True,start_id=1) + _apartments(100,central=True,start_id=200,owner=False)
    start = datetime(2026,11,13,5,tzinfo=BISHKEK)
    first = plan_period(stock,period_start=start,rng=random.Random(1))
    used = {p.apartment.id for p in first}
    second = plan_period([a for a in stock if a.id not in used],period_start=start+timedelta(minutes=630),rng=random.Random(2))
    rows = first+second
    assert len(rows) == total and len({p.apartment.id for p in rows}) == total
    assert sum(p.apartment.seller_type=='realtor' for p in rows) == agents
    assert agents*5 <= total


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining_normal,allowed',[(11,False),(8,True)])
async def test_dispatch_allows_tenth_agent_only_when_ordinary_supply_is_short(repositories,remaining_normal,allowed):
    apartments,_,sessions = repositories
    now = publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc)+timedelta(hours=19)
    for i in range(49):
        agent=i>=40
        a=await apartments.upsert_discovered(make_ad(lalafo_id=9100000+i,
            seller_type='realtor' if agent else 'owner',owner_listing=not agent))
        await apartments.mark_published(a.id,chat_id=-1001,message_id=9100000+i,
            acknowledged_at=now-timedelta(minutes=40+i))
    for i in range(remaining_normal):
        await apartments.upsert_discovered(make_ad(lalafo_id=9200000+i))
    agent=await apartments.upsert_discovered(make_ad(lalafo_id=9300000,seller_type='realtor',owner_listing=False))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=agent.id,window_key='test-adaptive',sequence=1,scheduled_at=now-timedelta(minutes=1)))
    claimed=await InventoryRepository(sessions).claim_due(now=now)
    assert (claimed is not None)==allowed

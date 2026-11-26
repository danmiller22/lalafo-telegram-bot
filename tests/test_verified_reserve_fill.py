from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import update, select
from app.inventory import InventoryRepository, publication_day_start
from app.models import Apartment, ApartmentInventoryQueue
from tests.helpers import make_ad

@pytest.mark.asyncio
async def test_only_old_active_reserve_enters_empty_queue(repositories):
    apartments, _, sessions = repositories
    now = publication_day_start(datetime.now(timezone.utc)) + timedelta(hours=2)
    cards=[]
    for index, age, active in [(1,72,True),(2,12,True),(3,72,False)]:
        card=await apartments.upsert_discovered(make_ad(lalafo_id=990000+index,
            phone=f'+99655599000{index}', photo_urls=[f'https://img.example/reserve-{index}.jpg']))
        cards.append(card)
        async with sessions.begin() as s:
            await s.execute(update(Apartment).where(Apartment.id==card.id).values(
                publication_status='published', published_at=now-timedelta(hours=age),
                active=active, availability_status='active' if active else 'unavailable',
                last_seen_at=now))
    count=await InventoryRepository(sessions).schedule_period(now=now)
    assert count==1
    async with sessions() as s:
        ids=list(await s.scalars(select(ApartmentInventoryQueue.apartment_id).where(
            ApartmentInventoryQueue.status=='queued')))
    assert ids==[cards[0].id]

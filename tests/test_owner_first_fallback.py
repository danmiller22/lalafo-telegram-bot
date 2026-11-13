from datetime import datetime, timedelta, timezone
import random

import pytest
from sqlalchemy import select

from app.inventory import InventoryRepository, plan_period, publication_day_start
from app.models import ApartmentInventoryQueue
from scripts.scrape_publish import select_owners_then_realtors
from tests.helpers import make_ad
from tests.test_inventory import _apartments


@pytest.mark.parametrize('blocked', ['+996 703 108 500', '0703-223-711'])
def test_agent_fallback_never_relaxes_blocked_contact_policy(blocked):
    blocked_ad = make_ad(lalafo_id=8800001, seller_type='realtor', owner_listing=False,
                          source_description='Контакты: ' + blocked)
    good = make_ad(lalafo_id=8800002, seller_type='realtor', owner_listing=False)
    selected = select_owners_then_realtors([blocked_ad, good], 5)
    assert [ad.lalafo_id for ad in selected] == [good.lalafo_id]
    agents = _apartments(2, central=True, start_id=8800001, owner=False)
    agents[0].source_description = 'Контакты: ' + blocked
    planned = plan_period(agents, period_start=datetime(2026, 11, 13, 5,
                          tzinfo=timezone(timedelta(hours=6))), rng=random.Random(7))
    assert [p.apartment.id for p in planned] == [agents[1].id]


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['+996 703 108 500', '0703-223-711'])
async def test_queued_agent_with_blocked_secondary_contact_is_never_claimed(repositories, blocked):
    apartments, _, sessions = repositories
    now = publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(hours=2)
    bad = await apartments.upsert_discovered(make_ad(lalafo_id=8800100,
        seller_type='realtor', owner_listing=False, source_description='Ещё номер: ' + blocked))
    good = await apartments.upsert_discovered(make_ad(lalafo_id=8800101,
        seller_type='realtor', owner_listing=False, phone='+996555001234'))
    async with sessions.begin() as session:
        for i, ad in enumerate((bad, good)):
            session.add(ApartmentInventoryQueue(apartment_id=ad.id, sequence=i,
                window_key='fallback', scheduled_at=now - timedelta(minutes=1)))
    claimed = await InventoryRepository(sessions).claim_due(now=now)
    assert claimed is not None and claimed.apartment_id == good.id
    async with sessions() as session:
        row = await session.scalar(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.apartment_id == bad.id))
        assert row.status == 'skipped'

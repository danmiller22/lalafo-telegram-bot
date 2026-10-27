import json
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.managed_cards import PREFIX, managed_bindings
from app.models import LalafoAutoReplyMeta, RequestedCardPromotion
from app.promotions import register_requested, promotion_work
from tests.helpers import make_ad
from tools.publish_owned_requested_cards import handoff_to_periodic_schedule


@pytest.mark.asyncio
async def test_two_hour_external_batch_preserves_source_mapping_without_queue_repeats(repositories):
    repo, _, sessions = repositories
    apartment = await repo.upsert_discovered(make_ad())
    now = datetime.now(timezone.utc)
    binding = {'profile_id': 18209276, 'ad_id': 117635496,
               'url': 'https://lalafo.kg/bishkek/ads/example-id-117635496',
               'enabled': True, 'repeat_every_hours': 3, 'repeat_while_active': True}
    async with sessions.begin() as session:
        await register_requested(session, apartment, now=now)
        session.add(LalafoAutoReplyMeta(key=PREFIX+str(apartment.id), value=json.dumps(binding)))
    manifest = {'repeat_every_hours': 2, 'externally_scheduled': True}
    await handoff_to_periodic_schedule(sessions, [(binding, apartment, make_ad())], manifest)
    await handoff_to_periodic_schedule(sessions, [(binding, apartment, make_ad())], manifest)
    async with sessions() as session:
        promotion = await session.get(RequestedCardPromotion, apartment.id)
        assert not promotion.enabled
        assert not await managed_bindings(session)
        due, _ = await promotion_work(session, now=now+timedelta(hours=6),
                                     day_start=now, day_end=now+timedelta(days=1))
        assert due == []
    links = await repo.managed_lalafo_sources()
    assert len(links) == 1
    assert links[0].managed_lalafo_ad_id == binding['ad_id']
    assert links[0].apartment.phone == apartment.phone

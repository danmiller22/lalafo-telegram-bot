from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Apartment,DailyFeaturedPublication,RequestedCardPromotion,RequestedCardPublication
from tests.helpers import make_ad
from tools import publish_manual_requested_card as manual


@pytest.mark.asyncio
async def test_manual_card_keeps_private_contact_and_rerun_does_not_send_twice(repositories,monkeypatch):
    sessions=repositories[2]
    settings=Settings(_env_file=None,callback_secret='manual-test-secret',telegram_group_id=-100123)
    ad=make_ad(lalafo_id=-110945099,phone='+996555999991',price=28000,district='Восток-5 мкр')
    ad.source_url='manual://telegram/110945099'
    data={'ad':ad.model_dump(mode='json'),'managed_ad_id':110945099,
        'managed_ad_url':'https://lalafo.kg/bishkek/ads/1-komnata-id-110945099','managed_publication_status':'active'}
    publish=AsyncMock(return_value=SimpleNamespace(message_id=81,date=datetime.now(timezone.utc)))
    monkeypatch.setattr(manual,'TelegramPublisher',lambda *a,**kw:SimpleNamespace(publish=publish))
    assert await manual.publish_card(data,sessions,object(),settings)==81
    assert await manual.publish_card(data,sessions,object(),settings)==81
    publish.assert_awaited_once()
    async with sessions() as s:
        apartment=await s.scalar(select(Apartment).where(Apartment.lalafo_id==ad.lalafo_id))
        assert apartment.phone==ad.phone and apartment.telegram_message_id==81
        link=await s.scalar(select(DailyFeaturedPublication).where(DailyFeaturedPublication.managed_lalafo_ad_id==110945099))
        assert link.source_apartment_id==apartment.id and link.telegram_message_id==81
        promotion=await s.get(RequestedCardPromotion,apartment.id)
        assert promotion is not None
        assert len(list((await s.scalars(select(RequestedCardPublication).where(RequestedCardPublication.apartment_id==apartment.id))).all()))==1

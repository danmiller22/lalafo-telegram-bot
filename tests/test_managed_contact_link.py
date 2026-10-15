import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import DailyFeaturedPublication
from tests.helpers import make_ad
from tools import link_requested_lalafo_cards as link


@pytest.mark.asyncio
async def test_new_account_updates_campaign_without_replacing_original_contact(repositories,monkeypatch,tmp_path):
    apartments,_,sessions=repositories
    source=await apartments.upsert_discovered(make_ad(lalafo_id=901010))
    await apartments.mark_published(source.id,chat_id=-100123,message_id=42)
    async with sessions.begin() as s:
        s.add(DailyFeaturedPublication(business_date=date(2026,10,15),slot=1,
            source_apartment_id=source.id,source_lalafo_id=source.lalafo_id,managed_lalafo_ad_id=910101,
            campaign_status='none',telegram_message_id=42,telegram_chat_id=-100123))
    monkeypatch.setattr(link,'get_settings',lambda:Settings(_env_file=None))
    monkeypatch.setattr(link,'create_engine_and_session',lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    path=tmp_path/'verified.json';path.write_text(json.dumps({'owner_profile_id':18193783,'cards':[
        {'source_id':source.lalafo_id,'managed_ad_id':910101,'managed_ad_url':'https://lalafo.kg/bishkek/ads/1-komnata-id-910101','campaign_id':81,'daily_kgs':213,'message_id':42,'photo_urls':source.photo_urls}]}))
    await link.main(str(path))
    async with sessions() as s:
        row=await s.scalar(select(DailyFeaturedPublication).where(DailyFeaturedPublication.managed_lalafo_ad_id==910101))
        assert row.source_apartment_id==source.id and row.source_lalafo_id==source.lalafo_id
        assert row.campaign_id=='81' and row.campaign_daily_budget==21300 and row.campaign_status=='started'
    # A different source must never acquire this managed ad's contact mapping.
    other=await apartments.upsert_discovered(make_ad(lalafo_id=901011,phone='+996555999992'))
    await apartments.mark_published(other.id,chat_id=-100123,message_id=43)
    data=json.loads(path.read_text());data['cards'][0]['source_id']=other.lalafo_id;path.write_text(json.dumps(data))
    with pytest.raises(AssertionError):
        await link.main(str(path))

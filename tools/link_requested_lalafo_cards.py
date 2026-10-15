"""Record verified owned ads against their original contact cards; no sends."""
import argparse,asyncio,json
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from sqlalchemy import select,func
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,DailyFeaturedPublication

async def main(manifest="tools/lalafo_requested_batch_20261014.json"):
    data=json.loads(Path(manifest).read_text())
    assert data['owner_profile_id'] in {18166976,18193783} and 1 <= len(data['cards']) <= 20
    engine,sessions=create_engine_and_session(get_settings().database_url)
    day=datetime.now(ZoneInfo('Asia/Bishkek')).date()
    try:
        async with sessions.begin() as session:
            slot=int(await session.scalar(select(func.max(DailyFeaturedPublication.slot)).where(DailyFeaturedPublication.business_date==day)) or 0)
            for card in data['cards']:
                source=await session.scalar(select(Apartment).where(Apartment.lalafo_id==card['source_id']))
                assert source is not None and source.phone and source.telegram_message_id
                row=await session.scalar(select(DailyFeaturedPublication).where(DailyFeaturedPublication.managed_lalafo_ad_id==card['managed_ad_id']))
                if row is not None:
                    assert row.source_lalafo_id==card['source_id']
                    row.campaign_id=str(card['campaign_id'])
                    row.campaign_status='started'
                    row.campaign_daily_budget=int(card.get('daily_kgs',210)*100)
                    row.lalafo_publication_status='active'
                    continue
                slot+=1
                session.add(DailyFeaturedPublication(business_date=day,slot=slot,source_apartment_id=source.id,
                    source_lalafo_id=source.lalafo_id,managed_lalafo_ad_id=card['managed_ad_id'],
                    managed_lalafo_ad_url=card['managed_ad_url'],campaign_id=str(card['campaign_id']),campaign_status='started',
                    campaign_daily_budget=int(card.get('daily_kgs',210)*100),lalafo_publication_status='active',managed_lalafo_uploaded_photos=len(card['photo_urls']),
                    telegram_message_id=card['message_id'],telegram_chat_id=source.telegram_chat_id))
            print('OWNED_REQUESTED_CARDS_LINKED='+str(len(data['cards'])))
    finally:await engine.dispose()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',default='tools/lalafo_requested_batch_20261014.json')
    asyncio.run(main(parser.parse_args().manifest))

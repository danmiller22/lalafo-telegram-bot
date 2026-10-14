"""Read-only evidence for publication duplicate investigations; no contacts or secrets."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from itertools import combinations

from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment
from app.state import _photo_keys, normalized_district, same_listing

async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    try:
        async with sessions() as session:
            rows = list((await session.scalars(select(Apartment).where(
                Apartment.published_at >= datetime.now(timezone.utc) - timedelta(days=7)
            ).order_by(Apartment.published_at.desc()).limit(600))).all())
        for row in rows:
            if row.rooms == 'studio' and row.price == 30000 and 'токол' in normalized_district(row.district):
                print('TARGET_CARD',json.dumps({'id':row.id,'source_id':row.lalafo_id,'source_url':row.source_url,'message_id':row.telegram_message_id,'published_at':row.published_at.isoformat(),'photos':row.photo_urls},ensure_ascii=False))
        for a,b in combinations(rows,2):
            overlap = _photo_keys(a.photo_urls) & _photo_keys(b.photo_urls)
            if overlap and a.rooms==b.rooms and normalized_district(a.district)==normalized_district(b.district):
                print('PHOTO_OVERLAP',json.dumps({'ids':[a.id,b.id],'message_ids':[a.telegram_message_id,b.telegram_message_id],'photos':[len(a.photo_urls),len(b.photo_urls)],'common':len(overlap),'detected':same_listing(a,b)}))
        print('AUDITED',len(rows))
    finally:
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())

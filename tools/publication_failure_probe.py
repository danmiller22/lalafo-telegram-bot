"""Read-only checks, emitting aggregate causes and HTTP metadata only."""
import asyncio,json
from collections import Counter
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock
from scripts.scrape_publish import apartment_to_ad
from app.telegram.publisher import TelegramPublisher
from app.security import TokenSigner
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,ApartmentInventoryQueue
from app.listing_policy import PUBLIC_ROOMS

async def main():
    engine,sessions=create_engine_and_session(get_settings().database_url)
    try:
        async with sessions() as s:
            rows=list((await s.scalars(select(Apartment).join(ApartmentInventoryQueue).where(
                ApartmentInventoryQueue.last_error=='TelegramPublishError',
                ApartmentInventoryQueue.status.in_(['queued','skipped']),
            ).order_by(ApartmentInventoryQueue.updated_at.desc()).limit(20))).all())
        counts=Counter();photos=Counter();validation=Counter()
        async with httpx.AsyncClient(timeout=8,follow_redirects=True) as h:
            for a in rows:
                counts['shared_offers' if a.rooms == 'shared' else 'whole_apartment']+=1
                bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                    send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1)]),
                    send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
                try:
                    await TelegramPublisher(bot,chat_id=-1,signer=TokenSigner('s'*32),bot_username='probe_bot',support_url='https://t.me/probe').publish(a.id,apartment_to_ad(a))
                    validation['publisher_passed']+=1
                except Exception as exc:
                    validation[type(exc).__name__+':'+str(exc)[:100]]+=1
                if a.photo_urls:
                    try:
                        async with h.stream('GET',a.photo_urls[0]) as r:
                            photos[str(r.status_code)+':'+r.headers.get('content-type','unknown').split(';')[0]]+=1
                    except Exception as e:photos[type(e).__name__]+=1
        print('FAILURE_PROBE='+json.dumps({'sample':len(rows),'categories':counts,'photo_http':photos,'local_validation':validation}),flush=True)
    finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

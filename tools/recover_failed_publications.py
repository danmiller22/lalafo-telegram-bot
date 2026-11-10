"""One-time recovery of definite failures; never replay ambiguous sends."""
import asyncio,json
from datetime import datetime,timezone,timedelta
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,ApartmentInventoryQueue,LalafoAutoReplyMeta,RequestedCardPublication
from app.inventory import discovery_period_start
from app.listing_policy import author_policy_eligible

async def main():
    engine,sessions=create_engine_and_session(get_settings().database_url)
    now=datetime.now(timezone.utc);marker='publication-recovery-media-shared-v1';count=0
    try:
        async with sessions.begin() as s:
            if s.bind.dialect.name=='postgresql':
                from sqlalchemy import text
                await s.execute(text('SELECT pg_advisory_xact_lock(731290519)'))
            if await s.get(LalafoAutoReplyMeta,marker):
                print('PUBLICATION_RECOVERY '+json.dumps({'already_applied':True,'requeued':0}));return
            rows=(await s.execute(select(ApartmentInventoryQueue,Apartment).join(Apartment).where(
                ApartmentInventoryQueue.status=='skipped',
                ApartmentInventoryQueue.last_error=='TelegramPublishError',
                ApartmentInventoryQueue.updated_at>=now-timedelta(days=2),
                Apartment.publication_status=='discovered',
                Apartment.telegram_message_id.is_(None),
                Apartment.id.not_in(select(RequestedCardPublication.apartment_id)),
            ).with_for_update())).all()
            for q,a in rows:
                if not author_policy_eligible(seller_type=a.seller_type,owner_listing=a.owner_listing):continue
                q.status='queued';q.attempts=0;q.claimed_at=None;q.last_error=None;q.scheduled_at=now
                count+=1
            s.add(LalafoAutoReplyMeta(key=marker,value=now.isoformat()))
        print('PUBLICATION_RECOVERY '+json.dumps({'requeued':count,'ambiguous_replayed':0}),flush=True)
    finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

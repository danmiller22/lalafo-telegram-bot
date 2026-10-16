"""Read-only diagnostics of managed repetitions and recent publication receipts."""
import asyncio,json
from datetime import datetime,timedelta,timezone
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.managed_cards import PREFIX
from app.inventory import publication_day_start,as_utc,publication_slots_due
from app.models import Apartment,ApartmentInventoryQueue,LalafoAutoReplyMeta,RequestedCardPromotion,RequestedCardPublication
from app.promotions import publications_in_window
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 try:
  now=datetime.now(timezone.utc);start=publication_day_start(now).astimezone(timezone.utc)
  async with sessions() as s:
   events=await publications_in_window(s,start,start+timedelta(days=1))
   active=list(await s.scalars(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='publishing')))
   print('MANAGED_DIAGNOSTIC_GLOBAL='+json.dumps({'now':now.isoformat(),'published_today':len(events),'slots_due':publication_slots_due(now),'last_sent':max((as_utc(e.published_at) for e in events),default=None).isoformat() if events else None,'in_flight':[{'apartment_id':r.apartment_id,'claimed_at':r.claimed_at.isoformat()} for r in active]}))
   markers=list(await s.scalars(select(LalafoAutoReplyMeta).where(LalafoAutoReplyMeta.key.startswith(PREFIX))))
   for marker in markers:
    b=json.loads(marker.value);aid=int(marker.key[len(PREFIX):]);a=await s.get(Apartment,aid);p=await s.get(RequestedCardPromotion,aid)
    print('MANAGED_DIAGNOSTIC_CARD='+json.dumps({'managed_ad_id':b['ad_id'],'enabled':b['enabled'],'active':a.active,'last_sent':a.published_at.isoformat() if a.published_at else None,'message_id':a.telegram_message_id,'expires_at':p.expires_at.isoformat()}))
   for event in events[:8]:
    a=await s.get(Apartment,event.apartment_id)
    print('RECENT_PUBLICATION='+json.dumps({'apartment_id':a.id,'seller_type':event.seller_type,'price':event.price,'district':event.district,'published_at':event.published_at.isoformat(),'message_id':a.telegram_message_id},ensure_ascii=False))
 finally:await engine.dispose()
asyncio.run(main())

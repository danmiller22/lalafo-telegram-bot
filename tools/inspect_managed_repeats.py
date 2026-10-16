"""Read-only summary of explicitly enrolled cards; no secrets/contact fields."""
import asyncio, json
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func
from app.config import get_settings
from app.database import create_engine_and_session
from app.managed_cards import managed_bindings, PREFIX
from app.models import LalafoAutoReplyMeta
from app.lalafo.client import LalafoClient
from app.managed_cards import refresh_managed_card
from tests.helpers import make_ad
from app.inventory import publication_day_start,as_utc,publication_slots_due
from app.models import Apartment,ApartmentInventoryQueue,RequestedCardPromotion,RequestedCardPublication
from app.promotions import publications_in_window
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 try:
  now=datetime.now(timezone.utc);start=publication_day_start(now).astimezone(timezone.utc);end=start+timedelta(days=1)
  async with sessions() as s:
   bindings={int(r.key[len(PREFIX):]):json.loads(r.value) for r in await s.scalars(select(LalafoAutoReplyMeta).where(LalafoAutoReplyMeta.key.startswith(PREFIX)))}
   events=await publications_in_window(s,start,end)
   active=list(await s.scalars(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='publishing')))
   print('MANAGED_DIAGNOSTIC_GLOBAL='+json.dumps({'now':now.isoformat(),'published_today':len(events),'slots_due':publication_slots_due(now),'last_sent':max((as_utc(e.published_at) for e in events),default=None).isoformat() if events else None,'in_flight':[{'apartment_id':r.apartment_id,'claimed_at':r.claimed_at.isoformat()} for r in active]}))
   for aid,b in bindings.items():
    a=await s.get(Apartment,aid);p=await s.get(RequestedCardPromotion,aid)
    daily=await s.scalar(select(func.count()).select_from(RequestedCardPublication).where(RequestedCardPublication.apartment_id==aid,RequestedCardPublication.published_at>=start,RequestedCardPublication.published_at<end))
    campaign=await s.scalar(select(func.count()).select_from(RequestedCardPublication).where(RequestedCardPublication.apartment_id==aid,RequestedCardPublication.published_at>=p.requested_at))
    print('MANAGED_DIAGNOSTIC_CARD='+json.dumps({'managed_ad_id':b['ad_id'],'apartment_id':aid,'active':a.active,'binding_enabled':b.get('enabled'),'last_sent':a.published_at.isoformat() if a.published_at else None,'message_id':a.telegram_message_id,'daily_sent':daily,'campaign_sent':campaign,'expires_at':p.expires_at.isoformat()}))
   async with LalafoClient(timeout=15,max_retries=0) as c:
    for aid,b in bindings.items():
     try:
      d=await c._get_json(f"https://lalafo.kg/api/search/v3/feed/details/{b['ad_id']}?expand=url")
      print('MANAGED_PUBLIC_FIELDS='+json.dumps({k:d.get(k) for k in ['id','user_id','status_id','category_id','city_id','currency','price']}))
      print('MANAGED_PUBLIC_PARAMS='+json.dumps([p for p in d.get('params',[]) if p.get('id')==69],ensure_ascii=False))
      try:await refresh_managed_card(c,b,make_ad());print('MANAGED_PUBLIC_VALID='+str(b['ad_id']))
      except Exception as exc:print('MANAGED_PUBLIC_FAILURE='+str(b['ad_id'])+' '+type(exc).__name__+' '+str(exc))
     except Exception as exc:print('MANAGED_PUBLIC_ERROR='+type(exc).__name__)
 finally:await engine.dispose()
asyncio.run(main())

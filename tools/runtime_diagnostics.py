"""Read-only verification of requested-card campaign receipts and queue state."""
import asyncio,json
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,ApartmentInventoryQueue,RequestedCardPromotion,RequestedCardPublication,ApartmentMatchDelivery,PaymentRequest
from app.promotions import publications_in_window,promotion_work
from app.inventory import publication_day_start
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 now=datetime.now(timezone.utc);start=publication_day_start(now).astimezone(timezone.utc)
 try:
  async with sessions() as s:
   events=await publications_in_window(s,start,start+timedelta(days=1))
   due,reserved=await promotion_work(s,now=now,day_start=start,day_end=start+timedelta(hours=21))
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'published_today':len(events),'due_promotions':len(due),'reserved':reserved}))
   rows=(await s.execute(select(Apartment,RequestedCardPromotion,ApartmentInventoryQueue).join(RequestedCardPromotion,Apartment.id==RequestedCardPromotion.apartment_id).outerjoin(ApartmentInventoryQueue,Apartment.id==ApartmentInventoryQueue.apartment_id))).all()
   for a,p,q in rows:
    count=await s.scalar(select(func.count()).select_from(RequestedCardPublication).where(RequestedCardPublication.apartment_id==a.id,RequestedCardPublication.published_at>=p.requested_at))
    print('RUNTIME_DIAGNOSTICS='+json.dumps({'source_id':a.lalafo_id,'active':a.active,'message_id':a.telegram_message_id,'campaign_sent':count,'expires_at':p.expires_at.isoformat(),'queue':q.status if q else None,'queue_error':q.last_error if q else None,'claimed_at':q.claimed_at.isoformat() if q and q.claimed_at else None}))
   for model, label in [(ApartmentInventoryQueue,"publication_queue"),(ApartmentMatchDelivery,"matching_queue"),(PaymentRequest,"payment_requests")]:
    counts=(await s.execute(select(model.status,func.count()).group_by(model.status))).all()
    print('RUNTIME_DIAGNOSTICS='+json.dumps({label:dict(counts)}))
   urls=(await s.scalars(select(Apartment.source_url).where(Apartment.active.is_(True),Apartment.last_seen_at>=now-timedelta(hours=48)))).all()
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'fresh_active_inventory':len(urls),'lalafo_inventory':sum('lalafo.kg/' in url for url in urls),'telegram_inventory':sum(url.startswith('https://t.me/') for url in urls)}))
   active=(await s.scalars(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='publishing'))).all()
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'active_queue_claims':len(active),'claims':[{'item_id':q.id,'claimed_at':q.claimed_at.isoformat() if q.claimed_at else None} for q in active]}))
 finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

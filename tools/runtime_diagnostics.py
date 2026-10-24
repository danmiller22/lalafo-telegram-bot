"""Read-only verification of requested-card campaign receipts and queue state."""
import asyncio,json,re
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import select,func
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,ApartmentInventoryQueue,RequestedCardPromotion,RequestedCardPublication,ApartmentMatchDelivery,PaymentRequest,DailyFeaturedPublication
from app.promotions import publications_in_window,promotion_work
from app.inventory import BISHKEK, MAX_PUBLICATIONS_PER_DAY, publication_day_start, as_utc, daily_realtor_target, publication_slots_due
from app.listing_quality import district_priority,rental_value_band
from app.payments.repository import ApartmentRepository
from app.state import ListingIndex
from scripts.scrape_publish import apartment_to_ad,is_substandard_structure
from scripts.publish_inventory import _valid
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 now=datetime.now(timezone.utc);start=publication_day_start(now).astimezone(timezone.utc)
 from tools.revenue_diagnostics import report
 await report(sessions)
 try:
  async with sessions() as s:
   events=await publications_in_window(s,start,start+timedelta(days=1))
   due,reserved=await promotion_work(s,now=now,day_start=start,day_end=start+timedelta(hours=21))
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'published_today':len(events),'due_promotions':len(due),'reserved':reserved}))
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'publication_timing':{
       'timezone':'Asia/Bishkek','slots_due':publication_slots_due(now),
       'counts_by_hour':dict(sorted(Counter(as_utc(e.published_at).astimezone(BISHKEK).strftime('%Y-%m-%dT%H:00') for e in events).items())),
       'latest_publication':as_utc(events[0].published_at).isoformat() if events else None,
   }}))
   rows=(await s.execute(select(Apartment,RequestedCardPromotion,ApartmentInventoryQueue).join(RequestedCardPromotion,Apartment.id==RequestedCardPromotion.apartment_id).outerjoin(ApartmentInventoryQueue,Apartment.id==ApartmentInventoryQueue.apartment_id))).all()
   for a,p,q in rows:
    count=await s.scalar(select(func.count()).select_from(RequestedCardPublication).where(RequestedCardPublication.apartment_id==a.id,RequestedCardPublication.published_at>=p.requested_at))
    print('RUNTIME_DIAGNOSTICS='+json.dumps({'source_id':a.lalafo_id,'active':a.active,'message_id':a.telegram_message_id,'campaign_sent':count,'expires_at':p.expires_at.isoformat(),'queue':q.status if q else None,'queue_error':q.last_error if q else None,'claimed_at':q.claimed_at.isoformat() if q and q.claimed_at else None}))
   for model, label in [(ApartmentInventoryQueue,"publication_queue"),(ApartmentMatchDelivery,"matching_queue"),(PaymentRequest,"payment_requests")]:
    counts=(await s.execute(select(model.status,func.count()).group_by(model.status))).all()
    print('RUNTIME_DIAGNOSTICS='+json.dumps({label:dict(counts)}))
   urls=(await s.scalars(select(Apartment.source_url).where(Apartment.active.is_(True),Apartment.last_seen_at>=now-timedelta(hours=48)))).all()
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'fresh_active_inventory':len(urls),'lalafo_inventory':sum('lalafo.kg/' in url for url in urls),'telegram_inventory':sum(url.startswith('https://t.me/') for url in urls)}))
   stock=list((await s.scalars(select(Apartment).where(
       Apartment.active.is_(True), Apartment.last_seen_at>=now-timedelta(hours=48),
       Apartment.availability_status!="unavailable",
   ).order_by(Apartment.last_seen_at.desc()))).all())
   valid=[(a,apartment_to_ad(a)) for a in stock]
   valid=[(a,ad) for a,ad in valid if _valid(ad,get_settings())[0]]
   fresh=[ad for a,ad in valid if a.publication_status=="discovered"]
   repeats=[ad for a,ad in valid if a.publication_status=="published"
            and a.published_at is not None and as_utc(a.published_at)<=now-timedelta(hours=48)]
   repo=ApartmentRepository(sessions)
   blocked_fresh=await repo.duplicate_candidate_ids(fresh)
   blocked_repeats=await repo.duplicate_candidate_ids(repeats,repost_before=now-timedelta(hours=48))
   fresh=[ad for ad in fresh if ad.lalafo_id not in blocked_fresh]
   repeats=[ad for ad in repeats if ad.lalafo_id not in blocked_repeats]
   unique=[];fresh_ids={ad.lalafo_id for ad in fresh}
   for ad in fresh+repeats:
    if not ListingIndex(unique).contains(ad):unique.append(ad)
   def by_seller(ads):
    return {kind:sum(ad.seller_type==kind for ad in ads) for kind in ("owner","realtor","unknown")}
   target_realtors=daily_realtor_target(now)
   print('RUNTIME_DIAGNOSTICS='+json.dumps({
       "seller_inventory":{
           "fresh_active_valid":by_seller([ad for a,ad in valid]),
           "unpublished_unique":by_seller([ad for ad in unique if ad.lalafo_id in fresh_ids]),
           "repeat_eligible_unique":by_seller([ad for ad in unique if ad.lalafo_id not in fresh_ids]),
           "total_publishable_unique":by_seller(unique),
           "daily_targets":{"owner":MAX_PUBLICATIONS_PER_DAY-target_realtors,"realtor":target_realtors},
           "repeat_after_hours":48,"requires_source_check_at_send":True,
       }}))
   previously_managed=set((await s.scalars(select(
       DailyFeaturedPublication.source_lalafo_id
   ))).all())
   def source_ids(value):
    if isinstance(value,dict):
     for key,item in value.items():
      if key=="source_id" and isinstance(item,int):yield item
      else:yield from source_ids(item)
    elif isinstance(value,list):
     for item in value:yield from source_ids(item)
   history_patterns=("lalafo_requested*.json","lalafo_source_history.json","managed_requested*.json",
                     "owned_telegram*.json","requested_card_promotions*.json")
   for pattern in history_patterns:
    for history_file in Path("tools").glob(pattern):
     try:previously_managed.update(source_ids(json.loads(history_file.read_text())))
     except (OSError,json.JSONDecodeError):pass
   managed_fingerprints=set((await s.scalars(select(Apartment.fingerprint).where(
       Apartment.lalafo_id.in_(previously_managed)
   ))).all()) if previously_managed else set()
   tomorrow_stock=list((await s.scalars(select(Apartment).where(
       Apartment.active.is_(True),
       Apartment.availability_status!="unavailable",
       Apartment.rooms=="1",
       Apartment.price.between(25_000,35_000),
       Apartment.phone!="",
       Apartment.last_seen_at>=now-timedelta(hours=48),
   ))).all())
   def target_group(item):
    text=f"{item.district or ''} {item.source_description or ''}".casefold().replace("ё","е")
    text=" ".join(text.replace("-"," ").split())
    if re.search(r"\bвосток\s*5\b",text):return "east5"
    central=("центр","цум","гум","филармони","бишкек парк","вефа","юг 2",
             "моссовет","манаса","золотой квадрат","азия молл","дордой плаза",
             "1000 мелочей","политех")
    return "central" if any(term in text for term in central) else None
   tomorrow_stock=[item for item in tomorrow_stock
                   if item.lalafo_id not in previously_managed
                   and item.fingerprint not in managed_fingerprints
                   and "codex_force_repost" not in item.source_url
                   and item.seller_type in {"owner","realtor"}
                   and len(item.photo_urls or [])>=4
                   and not is_substandard_structure(apartment_to_ad(item))
                   and target_group(item)]
   tomorrow_stock.sort(key=lambda item:(
       rental_value_band(item.price,item.rooms,item.source_description),
       -district_priority(item.district),item.price,
       -len(item.photo_urls or []),
       -as_utc(item.last_seen_at or item.updated_at).timestamp(),
   ))
   selected=[];selected_ids=set()
   for group,limit in (("east5",3),("central",4)):
    for item in tomorrow_stock:
     if len([chosen for chosen in selected if target_group(chosen)==group])>=limit:break
     if item.id in selected_ids or target_group(item)!=group:continue
     selected.append(item);selected_ids.add(item.id)
   print('TOMORROW_LALAFO_CANDIDATES='+json.dumps([{
       "source_id":item.lalafo_id,"source_url":item.source_url,
       "district":item.district,"target_group":target_group(item),
       "price":item.price,"seller_type":item.seller_type,
       "photos":len(item.photo_urls or []),
       "last_seen_at":as_utc(item.last_seen_at or item.updated_at).isoformat(),
   } for item in selected],ensure_ascii=False))
   active=(await s.scalars(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='publishing'))).all()
   print('RUNTIME_DIAGNOSTICS='+json.dumps({'active_queue_claims':len(active),'claims':[{'item_id':q.id,'claimed_at':q.claimed_at.isoformat() if q.claimed_at else None} for q in active]}))
 finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

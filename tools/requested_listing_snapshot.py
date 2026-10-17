"""Read public listing evidence for the owner's explicitly requested publication."""
import asyncio,json,os
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,or_
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, RequestedCardPublication
from app.state import normalized_district,same_listing

async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 try:
  async with sessions() as s:
   requested_message = os.environ.get('REQUESTED_TELEGRAM_MESSAGE_ID')
   if requested_message:
    message_id = int(requested_message)
    apartment_id = await s.scalar(select(RequestedCardPublication.apartment_id).where(
     RequestedCardPublication.telegram_chat_id == -1004389602150,
     RequestedCardPublication.telegram_message_id == message_id))
    a = await s.get(Apartment, apartment_id) if apartment_id else await s.scalar(select(Apartment).where(
     Apartment.telegram_chat_id == -1004389602150, Apartment.telegram_message_id == message_id))
    if a is None: raise ValueError('Requested card source not found')
    print('REQUESTED_LISTING='+json.dumps({'id':a.id,'source_id':a.lalafo_id,'source_url':a.source_url,
     'message_id':message_id,'price':a.price,'rooms':a.rooms,'district':a.district,
     'deposit':a.deposit,'photo_urls':a.photo_urls},ensure_ascii=False))
    return
   rows=list((await s.scalars(select(Apartment).where(or_(
    Apartment.telegram_message_id.in_([44537,44600]),
    (Apartment.publication_status=='published') & (Apartment.active.is_(True)) & (Apartment.published_at>=datetime.now(timezone.utc)-timedelta(days=2))
   )).order_by(Apartment.published_at.desc()))).all())
  selected=[]
  for a in rows:
   requested=a.telegram_message_id in [44537,44600]
   east5=normalized_district(a.district) in {'восток5'}
   if not requested and (not east5 or any(same_listing(a,b) for b in selected)):continue
   if east5:selected.append(a)
   print('REQUESTED_LISTING='+json.dumps({'id':a.id,'source_id':a.lalafo_id,'source_url':a.source_url,'message_id':a.telegram_message_id,'price':a.price,'rooms':a.rooms,'district':a.district,'deposit':a.deposit,'photo_urls':a.photo_urls,'requested_new':requested,'east5':east5},ensure_ascii=False))
 finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

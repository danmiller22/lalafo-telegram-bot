"""Resolve only the eleven owner-authorized active advertisements."""
import asyncio,json
from pathlib import Path
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,DailyFeaturedPublication,LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.lalafo.models import PHONE_SOURCE_VERSION

MANIFEST=Path(__file__).with_suffix('.json')
async def resolve(sessions):
 rows=json.loads(MANIFEST.read_text());repo=ApartmentRepository(sessions);resolved=[]
 async with sessions() as s:
  markers=list((await s.scalars(select(LalafoAutoReplyMeta).where(LalafoAutoReplyMeta.key.startswith('managed_requested_card:')))).all())
  bindings={}
  for marker in markers:
   try:
    b=json.loads(marker.value);bindings[int(b['ad_id'])]=int(marker.key.rsplit(':',1)[1])
   except (ValueError,KeyError,TypeError):continue
  for row in rows:
   a=None;method=None
   featured=await s.scalar(select(DailyFeaturedPublication).where(DailyFeaturedPublication.managed_lalafo_ad_id==row['managed_id']))
   if featured:
    a=await s.get(Apartment,featured.source_apartment_id) if featured.source_apartment_id else await s.scalar(select(Apartment).where(Apartment.lalafo_id==featured.source_lalafo_id));method='managed_binding'
   if a is None and row['managed_id'] in bindings:a=await s.get(Apartment,bindings[row['managed_id']]);method='requested_binding'
   if a is None:a=await repo.verified_source_by_photos(row['photo_urls'],excluded_lalafo_ids={r['managed_id'] for r in rows});method='identical_photos'
   valid=bool(a and a.phone and a.phone_source_version==PHONE_SOURCE_VERSION and a.owner_listing and a.seller_type=='owner' and a.rooms=='1')
   print('ACTIVE_REQUESTED='+json.dumps({'managed_id':row['managed_id'],'resolved':a is not None,'valid':valid,'method':method,'source_id':a.lalafo_id if a else None,'apartment_id':a.id if a else None,'source_url':a.source_url if a else None,'source_phone_verified':bool(a and a.phone and a.phone_source_version==PHONE_SOURCE_VERSION),'seller_type':a.seller_type if a else None,'owner_listing':a.owner_listing if a else None,'message_id':a.telegram_message_id if a else None},ensure_ascii=False),flush=True)
   resolved.append((row,a,valid))
 return resolved
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 try:await resolve(sessions)
 finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

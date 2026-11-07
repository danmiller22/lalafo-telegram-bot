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
   valid=bool(a and a.phone and a.phone_source_version==PHONE_SOURCE_VERSION and a.seller_type in {'owner','realtor'} and a.rooms=='1')
   print('ACTIVE_REQUESTED='+json.dumps({'managed_id':row['managed_id'],'resolved':a is not None,'valid':valid,'method':method,'source_id':a.lalafo_id if a else None,'apartment_id':a.id if a else None,'source_url':a.source_url if a else None,'source_phone_verified':bool(a and a.phone and a.phone_source_version==PHONE_SOURCE_VERSION),'seller_type':a.seller_type if a else None,'owner_listing':a.owner_listing if a else None,'message_id':a.telegram_message_id if a else None},ensure_ascii=False),flush=True)
   resolved.append((row,a,valid))
 return resolved
async def main():
 engine,sessions=create_engine_and_session(get_settings().database_url)
 try:
  await resolve(sessions)
  rows=json.loads(MANIFEST.read_text()); prices={r['price'] for r in rows}
  async with sessions() as session:
   candidates=list((await session.scalars(select(Apartment).where(Apartment.phone!='',Apartment.phone_source_version==PHONE_SOURCE_VERSION,Apartment.rooms=='1',Apartment.price.between(min(prices)-5000,max(prices)+5000)))).all())
   for a in candidates:
    print('PHOTO_CANDIDATE='+json.dumps({'apartment_id':a.id,'source_id':a.lalafo_id,'source_url':a.source_url,'price':a.price,'district':a.district,'photo_urls':a.photo_urls,'seller_type':a.seller_type,'owner_listing':a.owner_listing},ensure_ascii=False),flush=True)
 finally:await engine.dispose()
if __name__=='__main__':asyncio.run(main())

async def publish():
 import os
 from aiogram import Bot
 from app.security import TokenSigner
 from app.telegram.publisher import TelegramPublisher
 from scripts.scrape_publish import apartment_to_ad
 if os.environ.get('CONFIRM_CHAT_CARD_PUBLIC_SEND')!='true':raise ValueError('Explicit requested public send required')
 settings=get_settings()
 if settings.telegram_group_id!=-1004389602150:raise ValueError('Unexpected destination')
 engine,sessions=create_engine_and_session(settings.database_url);repo=ApartmentRepository(sessions)
 bot=Bot(settings.require_bot_token())
 try:
  await seed_confirmed_sources(sessions)
  rows=await resolve(sessions)
  if len(rows)!=11:raise ValueError('Unexpected requested batch')
  rows=[r for r in rows if r[2]]
  if not rows:raise ValueError('No verified original contacts')
  async with sessions() as session:
   task_markers=list((await session.scalars(select(LalafoAutoReplyMeta).where(LalafoAutoReplyMeta.key.startswith('authorized_active_20261107:')))).all())
   task_messages={int(m.value.split(':')[1]) for m in task_markers if m.value.startswith('published:')}
  publisher=TelegramPublisher(bot,chat_id=settings.telegram_group_id,signer=TokenSigner(settings.require_callback_secret()),bot_username=settings.telegram_bot_username,support_url=settings.support_bot_url,max_photos=settings.max_photos_per_apartment,request_timeout_seconds=35,allow_requested_realtors=True)
  for row,a,_ in rows:
   key=f'authorized_active_20261107:{row["managed_id"]}'
   async with sessions.begin() as s:
    prior=await s.get(LalafoAutoReplyMeta,key)
    if prior:
     if prior.value.startswith('published:'):
      print('ACTIVE_ALREADY_PUBLISHED='+json.dumps({'managed_id':row['managed_id'],'message_id':int(prior.value.split(':')[1])}),flush=True);continue
     raise RuntimeError('Reconcile earlier ambiguous send before retry')
    s.add(LalafoAutoReplyMeta(key=key,value='sending'))
   ad=apartment_to_ad(a).model_copy(update={'price':row['price'],'rooms':row['rooms'],'district':row['district'],'deposit':row['deposit'],'photo_urls':row['photo_urls'],'source_description':row['description']})
   await repo.upsert_discovered(ad,discovery_priority=True)
   if a.telegram_message_id in task_messages:
    from app.telegram.formatting import format_public_apartment,is_confirmed_owner
    from app.telegram.keyboards import apartment_keyboard
    from aiogram.exceptions import TelegramBadRequest
    try:
     await bot.edit_message_text(chat_id=settings.telegram_group_id,message_id=a.telegram_message_id,text=format_public_apartment(ad,bot_username=settings.telegram_bot_username,wanted_deep_link=True),parse_mode='HTML',reply_markup=apartment_keyboard(a.id,phone=ad.phone,confirmed_owner=is_confirmed_owner(ad),signer=publisher.signer,bot_username=publisher.bot_username,support_url=publisher.support_url))
    except TelegramBadRequest as exc:
     if 'message is not modified' not in str(exc):raise
    await repo.save_managed_lalafo_source(managed_lalafo_ad_id=row['managed_id'],managed_lalafo_ad_url=row['url'],apartment=a)
    async with sessions.begin() as session:
     marker=await session.get(LalafoAutoReplyMeta,key);marker.value=f'published:{a.telegram_message_id}'
    print('ACTIVE_ALREADY_PUBLISHED='+json.dumps({'managed_id':row['managed_id'],'message_id':a.telegram_message_id,'duplicate_reused':True}),flush=True)
    continue
   sent=await publisher.publish(a.id,ad)
   task_messages.add(sent.message_id)
   await repo.mark_published(a.id,chat_id=settings.telegram_group_id,message_id=sent.message_id,acknowledged_at=sent.date)
   await repo.save_managed_lalafo_source(managed_lalafo_ad_id=row['managed_id'],managed_lalafo_ad_url=row['url'],apartment=a)
   async with sessions.begin() as s:
    marker=await s.get(LalafoAutoReplyMeta,key);marker.value=f'published:{sent.message_id}'
   print('ACTIVE_PUBLISHED='+json.dumps({'managed_id':row['managed_id'],'message_id':sent.message_id,'chat_id':settings.telegram_group_id,'photos':min(len(row['photo_urls']),publisher.max_photos)}),flush=True)
 finally:
  await bot.session.close();await engine.dispose()


async def seed_confirmed_sources(sessions):
 import re
 from app.lalafo.models import LalafoAd
 from tools.lalafo_campaign_session import load_session
 path=Path(__file__).with_name('active_contacts_20261107.json')
 envelope=json.loads(path.read_text())
 confirmed=await load_session(envelope['sealed'],key_name=envelope['key_name'])
 rows={r['managed_id']:r for r in json.loads(MANIFEST.read_text())}
 repo=ApartmentRepository(sessions)
 for entry in confirmed:
  row=rows[entry['managed_id']];ad=LalafoAd.model_validate(entry['ad'])
  if not ad.source_url.startswith('https://t.me/') or ad.phone_source_version!=PHONE_SOURCE_VERSION or ad.photo_urls!=row['photo_urls']:
   raise ValueError('Confirmed source differs from the reviewed matching photographs')
  digits=re.sub(r'\D','',ad.phone)
  if len(digits)!=12 or digits[-9:] not in re.sub(r'\D','',ad.source_description):raise ValueError('Phone does not match supplied public source')
  a=await repo.upsert_discovered(ad,discovery_priority=True)
  await repo.save_managed_lalafo_source(managed_lalafo_ad_id=row['managed_id'],managed_lalafo_ad_url=row['url'],apartment=a)
  print('CONFIRMED_SOURCE_STORED='+json.dumps({'managed_id':row['managed_id'],'source_id':ad.lalafo_id,'source_url':ad.source_url}),flush=True)

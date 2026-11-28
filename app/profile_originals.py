"""Two-hour hosted profile scan; verified originals and send reservations persist."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import logging
import httpx
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select,text,func
from app.inventory import INVENTORY_CLAIM_LOCK_ID,InventoryRepository,as_utc
from app.lalafo.public_feed import PublicFeed,parsed_public_ad
from app.models import Apartment,ApartmentInventoryQueue,RentalFeedState,LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.promotions import external_repeat_receipt_key
from app.rental_feed import claim_cycle,finish_cycle
from app.security import TokenSigner
from app.state import normalized_district,ListingIndex
from app.telegram.publisher import TelegramPublisher,TelegramPublishAmbiguousError
from scripts.publish_inventory import _valid
from scripts.scrape_publish import apartment_to_ad

logger=logging.getLogger(__name__)


def shared_photos(own,original):
    def photos(ad):return {PurePosixPath(urlsplit(url).path).name.casefold() for url in ad.photo_urls}
    return len(photos(own)&photos(original))


def same_apartment(own,original):
    if shared_photos(own,original)<2:return False
    if own.rooms!=original.rooms or abs(own.price-original.price)>max(own.price,original.price)*0.1:return False
    if normalized_district(own.district)!=normalized_district(original.district):return False
    def area(ad):
        for p in ad.source_params:
            if str(p.get('id'))=='70' or 'площадь' in str(p.get('name','')).casefold():
                return str(p.get('value') or '')
    a,b=area(own),area(original)
    return not a or not b or a==b


async def reserve_original(sessions,apartment):
    now=datetime.now(timezone.utc)
    async with sessions.begin() as session:
        if session.bind.dialect.name=='postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'),{'lock_id':INVENTORY_CLAIM_LOCK_ID})
        marker=await session.get(RentalFeedState,'original_send:'+str(apartment.id),with_for_update=True)
        if marker and marker.value.get('state') in {'reserved','uncertain'}:return None
        if marker and marker.value.get('last_sent_at') and datetime.fromisoformat(marker.value['last_sent_at'])>now-timedelta(hours=2):return None
        recent=list((await session.scalars(select(Apartment).where(Apartment.published_at>now-timedelta(hours=2)))).all())
        if ListingIndex(recent).contains(apartment_to_ad(apartment)):return None
        busy=int(await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='publishing')) or 0)
        if busy:return None
        queued=list((await session.scalars(select(ApartmentInventoryQueue).where(ApartmentInventoryQueue.apartment_id==apartment.id,ApartmentInventoryQueue.status=='queued'))).all())
        for row in queued:
            row.status='skipped';row.last_error='owned_original_reserved'
        row=ApartmentInventoryQueue(apartment_id=apartment.id,scheduled_at=now,
            window_key='profile-original:'+str(int(now.timestamp())),sequence=apartment.id,
            status='uncertain',claimed_at=now,attempts=1)
        session.add(row)
        value={'state':'reserved','reserved_at':now.isoformat()}
        if marker:marker.value=value
        else:session.add(RentalFeedState(key='original_send:'+str(apartment.id),value=value))
        await session.flush()
        return row.id


async def send_original(runtime,ad):
    settings=runtime.workflow_data['settings'];repo=runtime.workflow_data['apartments'];sessions=repo.sessions
    if not _valid(ad,settings)[0]:return False
    apartment=await repo.upsert_discovered(ad)
    queue_id=await reserve_original(sessions,apartment)
    if queue_id is None:return False
    publisher=TelegramPublisher(runtime.bot,chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,max_photos=settings.max_photos_per_apartment,
        card_delay_seconds=settings.showcase_card_interval_seconds)
    try:
        async with asyncio.timeout(90):
            message=await publisher.publish(apartment.id,ad)
        # Store the acknowledgement and durable terminal reservation together,
        # before any optional metadata update. Restart cannot resend the card.
        now=getattr(message,'date',None) or datetime.now(timezone.utc)
        async with sessions.begin() as session:
            marker=await session.get(RentalFeedState,'original_send:'+str(apartment.id),with_for_update=True)
            marker.value={'state':'published','last_sent_at':as_utc(now).isoformat(),
                'message_id':message.message_id,'chat_id':settings.telegram_group_id}
            row=await session.get(ApartmentInventoryQueue,queue_id)
            row.status='published';row.published_at=now
            session.add(LalafoAutoReplyMeta(key=external_repeat_receipt_key(settings.telegram_group_id,message.message_id),value='hosted-profile-original'))
            from app.promotions import record_delivery
            await record_delivery(session,apartment,chat_id=settings.telegram_group_id,message_id=message.message_id,published_at=now)
        await repo.mark_published(apartment.id,chat_id=settings.telegram_group_id,message_id=message.message_id,acknowledged_at=now)
        return True
    except (Exception,asyncio.CancelledError) as exc:
        # A send without an acknowledged card is never repeated blindly.
        async with sessions.begin() as session:
            marker=await session.get(RentalFeedState,'original_send:'+str(apartment.id),with_for_update=True)
            if marker.value.get('state')!='published':marker.value={**marker.value,'state':'uncertain','error':type(exc).__name__}
            row=await session.get(ApartmentInventoryQueue,queue_id)
            if row.status!='published':row.status='uncertain';row.last_error=type(exc).__name__
        raise


async def optional_detail(source, source_id):
    try:
        return await source.detail(source_id)
    except ValueError:
        return None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404,410):return None
        raise


async def scan_profile(runtime):
    settings=runtime.workflow_data['settings'];repo=runtime.workflow_data['apartments'];sessions=repo.sessions
    if not await claim_cycle(sessions,'profile_originals',interval=timedelta(hours=2),lease=timedelta(minutes=20)):return
    found=published=active=0
    try:
        profile=int(settings.rental_originals_profile_id)
        mappings={r.managed_lalafo_ad_id:r.apartment for r in await repo.managed_lalafo_sources()}
        async with sessions() as session:
            previous=await session.get(RentalFeedState,'profile_originals')
            page_start=int(previous.value.get('next_search_page',1))
        search_cache={}

        async with PublicFeed(proxy_urls=settings.lalafo_proxy_url, verify_routes=True) as source:
            own_items=[]
            for page in range(1,11):
                data=await source.get('/api/search/v3/feed/active/'+str(profile),{'expand':'url','per-page':100,'page':page})
                if not isinstance(data.get('items'),list):raise ValueError('InvalidProfileFeed')
                # Never treat an ignored profile filter as a valid profile scan.
                if any(int(r.get('user_id') or 0)!=profile for r in data['items']):raise ValueError('ProfileOwnerMismatch')
                own_items.extend(r for r in data['items'] if r.get('status_id')==2 and r.get('category_id')==2044)
                if len(data['items'])<100:break
            active=len(own_items)
            own_ids={int(r['id']) for r in own_items}
            for raw in own_items:
                # Own ads can hide the public phone. It is never used as the
                # original contact, but rooms/photos/location must be parsed.
                if not raw.get('params'):
                    raw=await optional_detail(source,int(raw['id']))
                    if raw is None:continue
                if int(raw.get('user_id') or 0)!=profile:raise ValueError('ProfileOwnerMismatch')
                own=parsed_public_ad(raw, include_contact=False)
                mapped=mappings.get(own.lalafo_id)
                if mapped is None:
                    mapped=await repo.verified_source_by_photos(own.photo_urls,excluded_lalafo_ids=own_ids)
                original=None
                if mapped is not None:
                    original_raw=await optional_detail(source,mapped.lalafo_id)
                    if original_raw is not None and original_raw.get('user_id')!=profile:
                        try:original=parsed_public_ad(original_raw)
                        except ValueError:pass
                if original is None:
                    # Search the same room/price segment, then require unique
                    # exact-photo evidence. No phone is inferred from similarity.
                    from app.config import DEFAULT_SEARCH_URL
                    candidates=[]
                    for page in range(page_start,page_start+12):
                        if page not in search_cache:
                            search_cache[page]=await source.search(DEFAULT_SEARCH_URL.replace('/owner',''),page)
                        data=search_cache[page]
                        for item in data.get('items',[]):
                            if item.get('id') in own_ids:continue
                            try:ad=parsed_public_ad(item,include_contact=False)
                            except ValueError:continue
                            if shared_photos(own,ad)<2:continue
                            original_raw=await optional_detail(source,ad.lalafo_id)
                            if original_raw is None or original_raw.get('user_id')==profile:continue
                            try:ad=parsed_public_ad(original_raw)
                            except ValueError:continue
                            if same_apartment(own,ad) and _valid(ad,settings)[0]:candidates.append(ad)
                    unique={ad.lalafo_id:ad for ad in candidates}
                    if len(unique)==1:
                        original=next(iter(unique.values()))
                        original_raw=await optional_detail(source,original.lalafo_id)
                        original=parsed_public_ad(original_raw) if original_raw is not None else None
                if original is None or not same_apartment(own,original) or not _valid(original,settings)[0]:continue
                found+=1
                apartment=await repo.upsert_discovered(original)
                await repo.save_managed_lalafo_source(managed_lalafo_ad_id=own.lalafo_id,managed_lalafo_ad_url=own.source_url,apartment=apartment)
                published+=int(await send_original(runtime,original))
        await finish_cycle(sessions,'profile_originals',{'profile_id':profile,'active_ads':active,'originals_found':found,'published':published,
            'search_pages':len(search_cache),'next_search_page':1 if page_start>=25 else page_start+12})
    except asyncio.CancelledError:raise
    except Exception as exc:
        await finish_cycle(sessions,'profile_originals',{'active_ads':active,'originals_found':found,'published':published},error=(type(exc).__name__ + (':HTTP'+str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else '')))
        logger.error('Profile original scan failed: %s',type(exc).__name__)


async def run_profile_originals(runtime):
    while True:
        try:await scan_profile(runtime)
        except asyncio.CancelledError:raise
        except Exception as exc:logger.error('Profile worker recovered: %s',type(exc).__name__)
        await asyncio.sleep(60)

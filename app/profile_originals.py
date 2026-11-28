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
from app.photo_match import PhotoMatcher
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


def matching_terms(own,original):
    if own.rooms!=original.rooms or abs(own.price-original.price)>max(own.price,original.price)*0.1:return False
    if normalized_district(own.district)!=normalized_district(original.district):return False
    def area(ad):
        for p in ad.source_params:
            if str(p.get('id'))=='70' or 'площадь' in str(p.get('name','')).casefold():
                return str(p.get('value') or '')
    a,b=area(own),area(original)
    return not a or not b or a==b


def same_apartment(own,original):
    return shared_photos(own,original)>=2 and matching_terms(own,original)


async def verified_photos(own,original,matcher):
    return matching_terms(own,original) and (shared_photos(own,original)>=2
        or await matcher.matches(own.photo_urls,original.photo_urls))


async def reserve_original(sessions,apartment):
    now=datetime.now(timezone.utc)
    async with sessions.begin() as session:
        if session.bind.dialect.name=='postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'),{'lock_id':INVENTORY_CLAIM_LOCK_ID})
        marker=await session.get(RentalFeedState,'original_send:'+str(apartment.id),with_for_update=True)
        if marker and marker.value.get('state') in {'reserved','uncertain'}:return None
        if marker and marker.value.get('last_sent_at') and datetime.fromisoformat(marker.value['last_sent_at'])>now-timedelta(hours=2):return None
        legacy_uncertain=list((await session.scalars(select(Apartment).where(
            (Apartment.publication_status=='uncertain') | Apartment.id.in_(
                select(ApartmentInventoryQueue.apartment_id).where(ApartmentInventoryQueue.status=='uncertain'))
        ))).all())
        if ListingIndex(legacy_uncertain).contains(apartment_to_ad(apartment)):return None
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
            from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION
            live=await session.get(Apartment,apartment.id,with_for_update=True)
            if live.telegram_message_id and live.telegram_chat_id and live.published_at:
                await record_delivery(session,live,chat_id=live.telegram_chat_id,
                    message_id=live.telegram_message_id,published_at=live.published_at)
            await record_delivery(session,live,chat_id=settings.telegram_group_id,
                message_id=message.message_id,published_at=now)
            live.telegram_chat_id=settings.telegram_group_id
            live.telegram_message_id=message.message_id
            live.published_at=now
            live.publication_status='published'
            live.active=True
            live.availability_status='active'
            live.availability_checked_at=now
            live.availability_reason='publication_age_window'
            live.keyboard_version=APARTMENT_KEYBOARD_VERSION
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


async def original_from_catalog(own,mapped,repo,source,matcher,own_ids,settings,stats):
    async def current(apartment):
        raw=await optional_detail(source,apartment.lalafo_id)
        if raw is None:
            stats['unavailable_originals']+=1
            return None
        if int(raw.get('id') or 0) in own_ids:return None
        try:ad=parsed_public_ad(raw)
        except ValueError:return None
        if _valid(ad,settings)[0] and await verified_photos(own,ad,matcher):return ad
    if mapped is not None:
        ad=await current(mapped)
        if ad is not None:return ad
    async with repo.sessions() as session:
        rows=list((await session.scalars(select(Apartment).where(
            Apartment.lalafo_id>0,Apartment.lalafo_id.not_in(own_ids),
            Apartment.rooms==own.rooms,Apartment.active.is_(True),
            Apartment.availability_status!='unavailable',
            Apartment.price.between(int(own.price/1.1),int(own.price*1.1)),
        ).order_by(Apartment.last_seen_at.desc()))).all())
    found={}
    attempted=0
    for row in rows:
        candidate=apartment_to_ad(row)
        if not matching_terms(own,candidate) or not _valid(candidate,settings)[0]:continue
        attempted+=1
        if attempted>30:break
        if not await verified_photos(own,candidate,matcher):continue
        ad=await current(row)
        if ad is not None:found[ad.lalafo_id]=ad
    owners=[ad for ad in found.values() if ad.owner_listing and ad.seller_type=='owner']
    if len(owners)==1:return owners[0]
    return next(iter(found.values())) if len(found)==1 else None


async def scan_profile(runtime):
    settings=runtime.workflow_data['settings'];repo=runtime.workflow_data['apartments'];sessions=repo.sessions
    if not await claim_cycle(sessions,'profile_originals',interval=timedelta(hours=2),lease=timedelta(minutes=20),version=2):return
    found=published=active=0
    matching_stats={"unavailable_originals":0}
    try:
        profile=int(settings.rental_originals_profile_id)
        mappings={r.managed_lalafo_ad_id:r.apartment for r in await repo.managed_lalafo_sources()}
        async with sessions() as session:
            previous=await session.get(RentalFeedState,'profile_originals')
            page_start=int(previous.value.get('next_search_page',1))
        search_cache={}

        async with asyncio.timeout(900), PublicFeed(proxy_urls=settings.lalafo_proxy_url, verify_routes=True) as source, PhotoMatcher(
                sessions) as matcher:
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
                original=await original_from_catalog(own,mapped,repo,source,matcher,own_ids,settings,matching_stats)
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
                            plausible = (abs(own.price-ad.price)<=max(own.price,ad.price)*0.1
                                and normalized_district(own.district)==normalized_district(ad.district))
                            if shared_photos(own,ad)<2 and not plausible:continue
                            if shared_photos(own,ad)<2 and not await matcher.matches(own.photo_urls,ad.photo_urls):continue
                            original_raw=await optional_detail(source,ad.lalafo_id)
                            if original_raw is None or original_raw.get('user_id')==profile:continue
                            try:ad=parsed_public_ad(original_raw)
                            except ValueError:continue
                            if _valid(ad,settings)[0] and await verified_photos(own,ad,matcher):candidates.append(ad)
                    unique={ad.lalafo_id:ad for ad in candidates}
                    if len(unique)==1:
                        original=next(iter(unique.values()))
                        original_raw=await optional_detail(source,original.lalafo_id)
                        original=parsed_public_ad(original_raw) if original_raw is not None else None
                if original is None or not _valid(original,settings)[0] or not await verified_photos(own,original,matcher):continue
                found+=1
                async with sessions.begin() as session:
                    key='original_match:'+str(own.lalafo_id)
                    evidence=await session.get(RentalFeedState,key)
                    value={'original_id':original.lalafo_id,'minimum_matching_photos':2,
                        'method':'filenames' if shared_photos(own,original)>=2 else 'dual_perceptual_hash',
                        'checked_at':datetime.now(timezone.utc).isoformat()}
                    if evidence:evidence.value=value
                    else:session.add(RentalFeedState(key=key,value=value))
                apartment=await repo.upsert_discovered(original)
                await repo.save_managed_lalafo_source(managed_lalafo_ad_id=own.lalafo_id,managed_lalafo_ad_url=own.source_url,apartment=apartment)
                published+=int(await send_original(runtime,original))
        await finish_cycle(sessions,'profile_originals',{'profile_id':profile,'active_ads':active,'originals_found':found,'published':published,
            'search_pages':len(search_cache),'next_search_page':1 if page_start>=25 else page_start+12,
            'photo_matching':matcher.stats,**matching_stats})
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

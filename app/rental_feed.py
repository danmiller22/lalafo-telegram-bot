"""Persistent Koyeb source replenishment, independent of external runners."""
import asyncio
from datetime import datetime,timedelta,timezone
import logging
import httpx

from sqlalchemy import select,text,func
from app.config import INVENTORY_SEARCH_URLS
from app.models import Apartment,ApartmentInventoryQueue,RentalFeedState
from app.inventory import InventoryRepository,as_utc,discovery_period_start,publication_window_open
from app.lalafo.public_feed import PublicFeed,parsed_public_ad
from app.payments.repository import ApartmentRepository
from app.rental_market import daily_medians
from app.state import ListingIndex
from scripts.publish_inventory import _valid

logger=logging.getLogger(__name__)


async def claim_cycle(sessions,key,*,interval=timedelta(minutes=10),lease=timedelta(minutes=8),version=None):
    now=datetime.now(timezone.utc)
    async with sessions.begin() as session:
        if session.bind.dialect.name=='postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(731290515)'))
        row=await session.get(RentalFeedState,key,with_for_update=True)
        value=row.value if row else {}
        if value.get('lease_until') and datetime.fromisoformat(value['lease_until'])>now:return False
        same_version=version is None or value.get('version')==version
        if same_version and value.get('last_success_at') and datetime.fromisoformat(value['last_success_at'])+interval>now:return False
        if not same_version:value={k:v for k,v in value.items() if k!='last_success_at'}
        value={**value,'last_started_at':now.isoformat(),'lease_until':(now+lease).isoformat(),'state':'running','version':version}
        if row:row.value=value
        else:session.add(RentalFeedState(key=key,value=value))
    return True


async def finish_cycle(sessions,key,result,*,error=None):
    now=datetime.now(timezone.utc)
    async with sessions.begin() as session:
        row=await session.get(RentalFeedState,key,with_for_update=True)
        value={**(row.value if row else {}),**result,'state':'error' if error else 'waiting',
               'last_error':error,'last_completed_at':now.isoformat(),'lease_until':None}
        if not error:value['last_success_at']=now.isoformat()
        if row:row.value=value
        else:session.add(RentalFeedState(key=key,value=value))


async def refill(runtime):
    settings=runtime.workflow_data['settings']
    apartments=runtime.workflow_data['apartments']
    sessions=apartments.sessions
    async with sessions() as session:
        before=int(await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='queued')) or 0)
    interval=timedelta(minutes=2 if before<20 else 10)
    if not await claim_cycle(sessions,'primary_feed',interval=interval,version=2):return
    async with sessions() as session:
        previous=await session.get(RentalFeedState,'primary_feed')
        page_start=int(previous.value.get('next_page',1))
    stored=pages=details=0
    try:
        async with sessions.begin() as session:
            await daily_medians(session)
        async with sessions() as session:
            queued=int(await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='queued')) or 0)
            known=set((await session.scalars(select(Apartment.lalafo_id).where(Apartment.last_seen_at>=datetime.now(timezone.utc)-timedelta(hours=2)))).all())
        if queued<40:
            seen=set(known)
            accepted=[]
            semaphore=asyncio.Semaphore(3)
            async with PublicFeed(proxy_urls=settings.lalafo_proxy_url, verify_routes=True) as source:
                async def read(raw):
                    nonlocal details
                    if raw.get('status_id')!=2 or raw.get('hide_phone'):return None
                    try:
                        ad=parsed_public_ad(raw)
                        allowed,reason=_valid(ad,settings)
                        if allowed:return ad if ad.photo_urls and ad.seller_type in {'owner','realtor'} else None
                        if reason not in {'phone','rooms','wrong_category','wrong_city'}:return None
                        if reason=='rooms' and raw.get('params'):return None
                        raise ValueError('SearchNeedsDetail')
                    except ValueError:
                        async with semaphore:
                            try:full=await source.detail(int(raw['id']))
                            except ValueError:return None
                            except httpx.HTTPStatusError as exc:
                                if exc.response.status_code in (404,410):return None
                                raise
                            details+=1
                        try:ad=parsed_public_ad(full)
                        except ValueError:return None
                        return ad if ad.photo_urls and _valid(ad,settings)[0] and ad.seller_type in {'owner','realtor'} else None
                async with asyncio.timeout(360):
                    # Alternate page depths durably; repeated IDs are cached in DB.
                    page=page_start
                    for route in INVENTORY_SEARCH_URLS:
                        raw_page=await source.search(route,page)
                        if not isinstance(raw_page.get('items'),list):raise ValueError('InvalidSearchFeed')
                        pages+=1
                        raw_items=[]
                        for raw in raw_page['items']:
                            source_id=int(raw.get('id') or 0)
                            if source_id<=0 or source_id in seen:continue
                            seen.add(source_id)
                            price=int(raw.get('price') or 0)
                            if not 25000<=price<=45000:continue
                            raw_items.append(raw)
                        for ad in await asyncio.gather(*(read(raw) for raw in raw_items)):
                            if ad is None or ListingIndex(accepted).contains(ad):continue
                            accepted.append(ad)
                            apartment = await apartments.upsert_discovered(ad)
                            await apartments.set_availability(apartment.id, status="active",
                                checked_at=datetime.now(timezone.utc), reason="public_feed_status_2")
                            stored+=1
                        if stored:
                            await InventoryRepository(sessions).schedule_period()
                        if stored>=60:break
        inventory=InventoryRepository(sessions)
        await inventory.schedule_period()
        now=datetime.now(timezone.utc)
        boundary=as_utc(discovery_period_start(now))+timedelta(minutes=630)
        if not publication_window_open(boundary):boundary+=timedelta(hours=3)
        async with sessions() as session:
            reserve=int(await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=="queued")) or 0)
        if boundary<=now+timedelta(hours=6) or reserve<20:await inventory.schedule_period(now=boundary)
        await finish_cycle(sessions,'primary_feed',{'stored':stored,'search_pages':pages,'detail_requests':details,'next_page':1 if page_start>=30 else page_start+1})
    except asyncio.CancelledError:raise
    except Exception as exc:
        await finish_cycle(sessions,'primary_feed',{'stored':stored,'search_pages':pages,'detail_requests':details,'next_page':1 if page_start>=30 else page_start+1},error=(type(exc).__name__ + (':HTTP'+str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else '')))
        logger.error('Hosted Lalafo refill failed: %s',type(exc).__name__)


async def run_rental_feed(runtime):
    while True:
        try:await refill(runtime)
        except asyncio.CancelledError:raise
        except Exception as exc:logger.error('Hosted refill recovered: %s',type(exc).__name__)
        await asyncio.sleep(60)

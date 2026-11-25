"""Read-only aggregate publisher diagnostics, without contact data."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from collections import Counter

from sqlalchemy import select, text
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, ApartmentInventoryQueue


async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as session:
            await session.execute(text('SET TRANSACTION READ ONLY'))
            rows = list((await session.scalars(select(ApartmentInventoryQueue))).all())
            def utc(value):
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            stock = list((await session.execute(select(Apartment.rooms, Apartment.active, Apartment.availability_status, Apartment.published_at))).all())
            from app.inventory import ALLOWED_ROOMS, is_supported_source
            from app.listing_policy import seller_policy_eligible, listing_policy_eligible
            from app.state import ListingIndex
            ads = list((await session.scalars(select(Apartment))).all())
            published = [ad for ad in ads if ad.publication_status == 'published']
            fingers = {ad.fingerprint for ad in published}
            index = ListingIndex(ad for ad in published if ad.rooms in ALLOWED_ROOMS)
            ad_by_id = {ad.id: ad for ad in ads}
            excluded_fingerprints = {
                ad_by_id[row.apartment_id].fingerprint
                for row in rows if row.apartment_id in ad_by_id and (
                    row.status in ("queued", "publishing", "uncertain")
                    or row.last_error == "watermarked_photos"
                    or (row.last_error == "TelegramPublishError" and row.attempts >= 3
                        and utc(row.scheduled_at) > now)
                )
            }
            from app.inventory import InventoryRepository, discovery_period_start, plan_period
            free = []
            queue_by_ad = {row.apartment_id: row for row in rows}
            counts = Counter()
            for ad in ads:
                if ad.rooms not in ALLOWED_ROOMS or not ad.last_seen_at or utc(ad.last_seen_at) < now - timedelta(hours=24):
                    continue
                if ad.publication_status != 'discovered':
                    reason = 'status_' + ad.publication_status
                elif not ad.photo_urls:
                    reason = 'no_photos'
                elif not is_supported_source(ad):
                    reason = 'unsupported'
                elif ad.seller_type not in ('owner', 'realtor') or not listing_policy_eligible(ad):
                    reason = 'seller_price_or_contact'
                elif ad.fingerprint in fingers:
                    reason = 'exact_duplicate'
                elif ad.fingerprint in excluded_fingerprints:
                    reason = "reserved_fingerprint"
                elif index.contains(ad):
                    reason = 'similar_duplicate'
                elif ad.id in queue_by_ad:
                    row = queue_by_ad[ad.id]
                    reason = 'queue_' + row.status + '_' + (row.last_error or 'none')
                else:
                    reason = 'free_candidate'
                    free.append(ad)
                counts[reason] += 1
            print('PLAN_PROBE=' + json.dumps({
                'target': await InventoryRepository(sessions)._period_stock_target(session, now),
                'free_plan': len(plan_period(free, period_start=discovery_period_start(now))),
            }), flush=True)
            print('STOCK_STAGES=' + json.dumps(dict(counts)), flush=True)
            print('ROOM_DIAGNOSTICS=' + json.dumps({
                'available': dict(Counter(row.rooms for row in stock if row.active and row.availability_status != 'unavailable')),
                'recently_published': dict(Counter(row.rooms for row in stock if row.published_at and utc(row.published_at) > now - timedelta(hours=48))),
                'unavailable': dict(Counter(row.rooms for row in stock if row.availability_status == 'unavailable')),
            }), flush=True)
            print('QUEUE_DIAGNOSTICS=' + json.dumps({
                'now': now.isoformat(),
                'statuses': dict(Counter(row.status for row in rows)),
                'queued_errors': dict(Counter(row.last_error or 'none' for row in rows if row.status == 'queued')),
                'recent_skipped_errors': dict(Counter(row.last_error or 'none' for row in rows if row.status == 'skipped' and utc(row.updated_at) > now - timedelta(hours=2))),
                'queued': [{'at': utc(row.scheduled_at).isoformat(), 'error': row.last_error, 'attempts': row.attempts, 'window': row.window_key} for row in rows if row.status == 'queued'],
                'publishing_claims': [{'claimed_at': utc(row.claimed_at).isoformat() if row.claimed_at else None, 'attempts':row.attempts} for row in rows if row.status == 'publishing'],
            }), flush=True)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

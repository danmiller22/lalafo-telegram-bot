"""Read-only aggregate publisher diagnostics, without contact data."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from collections import Counter

from sqlalchemy import select, text
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import ApartmentInventoryQueue, ApartmentDiscoveryRun, Apartment
from app.inventory import InventoryRepository, discovery_period_start, publication_day_start, PUBLICATION_PERIOD_MINUTES, as_utc
from app.promotions import publications_in_window


async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as session:
            await session.execute(text('SET TRANSACTION READ ONLY'))
            rows = list((await session.scalars(select(ApartmentInventoryQueue))).all())
            runs=list((await session.scalars(select(ApartmentDiscoveryRun))).all())
            period=discovery_period_start(now).astimezone(timezone.utc)
            start=publication_day_start(now).astimezone(timezone.utc)
            stock_target=await InventoryRepository(sessions)._period_stock_target(session,now)
            sent=list((await session.scalars(select(Apartment).where(Apartment.publication_status=='published',Apartment.published_at>=start))).all())
            ordinary=await publications_in_window(session,start,start+timedelta(days=1),include_external_repeats=False)
            inventory=list((await session.scalars(select(Apartment).where(Apartment.active.is_(True),Apartment.last_seen_at>=now-timedelta(hours=48),Apartment.rooms.in_(('1','studio')),Apartment.price.between(25000,45000)))).all())
            def utc(value):
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            print('QUEUE_DIAGNOSTICS=' + json.dumps({
                'now': now.isoformat(),
                'stock_target':stock_target,
                'period_published_apartments':sum(as_utc(x.published_at)>=period for x in sent),
                'ordinary_today':len(ordinary),
                'ordinary_in_period':sum(as_utc(x.published_at)>=period for x in ordinary),
                'fresh_inventory_by_status':dict(Counter(x.publication_status for x in inventory)),
                'discovery_runs':[{'key':x.period_key,'status':x.status,'lease':str(x.lease_until),'completed':str(x.completed_at),'queued':x.queued_count,'error':x.last_error} for x in runs],
                'uncertain_errors':dict(Counter(x.last_error for x in rows if x.status=='uncertain')),
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

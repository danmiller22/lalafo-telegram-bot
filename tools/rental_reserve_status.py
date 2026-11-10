"""Aggregate queue coverage without exposing listing or customer contacts."""
import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import ApartmentInventoryQueue
from app.inventory import publication_window_open, publication_slots_due, as_utc

async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as session:
            rows = list((await session.scalars(select(ApartmentInventoryQueue).where(
                ApartmentInventoryQueue.status == 'queued',
                ApartmentInventoryQueue.last_error.is_(None),
                ApartmentInventoryQueue.scheduled_at <= now + timedelta(hours=6),
            ))).all())
        # Count publication slots in the next six hours, respecting overnight
        # closure and the end of the daily plan, rather than assuming 24h supply.
        needed = 0
        for minute in range(360):
            at = now + timedelta(minutes=minute)
            before = at - timedelta(minutes=1)
            if publication_window_open(at):
                needed += max(0, publication_slots_due(at)-publication_slots_due(before))
        report = {'checked_at': now.isoformat(), 'horizon_hours': 6,
                  'needed_slots': needed, 'ready_queue': len(rows),
                  'shortfall': max(0, needed-len(rows)),
                  'status': 'covered' if len(rows)>=needed else 'insufficient_stock'}
        print('RENTAL_RESERVE_STATUS '+json.dumps(report), flush=True)
        if report['shortfall']:
            print('::warning::Rental reserve is below the next six-hour plan; source supply or delivery errors need attention', flush=True)
    finally:
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())

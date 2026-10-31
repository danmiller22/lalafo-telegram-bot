"""Read-only aggregate publisher diagnostics, without contact data."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from collections import Counter

from sqlalchemy import select, text
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import ApartmentInventoryQueue


async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as session:
            await session.execute(text('SET TRANSACTION READ ONLY'))
            rows = list((await session.scalars(select(ApartmentInventoryQueue))).all())
            def utc(value):
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
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

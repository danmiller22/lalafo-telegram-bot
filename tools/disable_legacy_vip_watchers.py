"""Disable the two legacy hosted cutoff loops without touching Lalafo ads."""
import asyncio
import json

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import LalafoAutoReplyMeta

KEYS = (
    'requested_campaign_expiry_key_20261014_completed',
    'requested_campaign_expiry_key_20261015_three_completed',
)


async def main():
    engine, sessions = create_engine_and_session(get_settings().database_url)
    try:
        async with sessions.begin() as session:
            for key in KEYS:
                row = await session.get(LalafoAutoReplyMeta, key, with_for_update=True)
                if row is None:
                    session.add(LalafoAutoReplyMeta(key=key, value='cancelled_by_user'))
        async with sessions() as session:
            confirmed = all([await session.get(LalafoAutoReplyMeta, key) is not None for key in KEYS])
        if not confirmed:
            raise RuntimeError('Legacy watcher disable markers were not persisted')
        print(json.dumps({'legacy_vip_watchers_disabled': True, 'marker_count': len(KEYS)}))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

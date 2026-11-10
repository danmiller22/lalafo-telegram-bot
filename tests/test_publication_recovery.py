from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime,timezone
import pytest
from sqlalchemy import select
from app.models import ApartmentInventoryQueue
from tests.helpers import make_ad

@pytest.mark.asyncio
async def test_recovery_requeues_only_definite_failure_once(repositories,monkeypatch):
    from tools import recover_failed_publications as recovery
    apartments,_,sessions=repositories
    now=datetime.now(timezone.utc)
    first=await apartments.upsert_discovered(make_ad(lalafo_id=998810))
    uncertain=await apartments.upsert_discovered(make_ad(lalafo_id=998811))
    async with sessions.begin() as s:
        s.add_all([
            ApartmentInventoryQueue(apartment_id=first.id,status='skipped',last_error='TelegramPublishError',attempts=3,scheduled_at=now,window_key='test'),
            ApartmentInventoryQueue(apartment_id=uncertain.id,status='uncertain',last_error='TelegramPublishAmbiguousError',attempts=1,scheduled_at=now,window_key='test'),
        ])
    monkeypatch.setattr(recovery,'get_settings',lambda:SimpleNamespace(database_url='unused'))
    monkeypatch.setattr(recovery,'create_engine_and_session',lambda url:(SimpleNamespace(dispose=AsyncMock()),sessions))
    await recovery.main()
    await recovery.main()
    async with sessions() as s:
        rows={r.apartment_id:r for r in (await s.scalars(select(ApartmentInventoryQueue))).all()}
        assert rows[first.id].status=='queued' and rows[first.id].attempts==0
        assert rows[uncertain.id].status=='uncertain' and rows[uncertain.id].attempts==1

from __future__ import annotations

import pytest_asyncio
import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService


@pytest.fixture
def publication_clock(monkeypatch, request):
    """Exercise publishing in its active window, independently of CI wall time."""
    from app import inventory
    from app.payments import repository
    from scripts import collect_lalafo_inventory

    instant = datetime.now(ZoneInfo("Asia/Bishkek")).replace(
        hour=10, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)

    class PublicationDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    for module in (inventory, repository, collect_lalafo_inventory, request.module):
        monkeypatch.setattr(module, "datetime", PublicationDateTime, raising=False)


@pytest_asyncio.fixture
async def repositories():
    engine, sessions = create_engine_and_session("sqlite:///:memory:")
    await init_db(engine)
    apartments = ApartmentRepository(sessions)
    payments = PaymentRepository(sessions)
    try:
        yield apartments, payments, sessions
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def service(repositories):
    apartments, payments, _ = repositories
    return PaymentService(apartments, payments, admin_user_id=999)

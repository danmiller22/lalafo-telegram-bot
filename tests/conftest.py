from __future__ import annotations

import pytest_asyncio

from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService


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

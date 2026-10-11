"""Exercise production row locks on an isolated PostgreSQL schema."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import init_db, normalize_database_url
from app.matching.repository import MatchingRepository
from app.models import Apartment, PaymentHistory
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payment_plans import WEEK_PLAN
from tests.helpers import make_ad


pytestmark = [pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")]


@pytest_asyncio.fixture
async def postgres_repositories():
    url = normalize_database_url(os.environ["POSTGRES_TEST_URL"])
    schema = "audit_" + uuid4().hex
    control = create_async_engine(url)
    engine = None
    try:
        async with control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        await init_db(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield ApartmentRepository(sessions), PaymentRepository(sessions), sessions
    finally:
        if engine is not None:
            await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await control.dispose()


async def test_postgres_collectors_concurrently_insert_same_source(postgres_repositories):
    apartments, _, sessions = postgres_repositories
    rows = await asyncio.gather(*(apartments.upsert_discovered(make_ad(), discovery_priority=(i == 0)) for i in range(12)))
    assert len({row.id for row in rows}) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count(Apartment.id))) == 1
    assert (await apartments.get(rows[0].id)).discovery_priority


async def test_postgres_checkout_reservation_is_stable_under_concurrent_clicks(postgres_repositories):
    apartments, payments, _ = postgres_repositories
    apartment = await apartments.upsert_discovered(make_ad())
    requests = await asyncio.gather(*(payments.submit(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN) for _ in range(10)))
    assert len({r.request.id for r in requests}) == 1
    reserved = await asyncio.gather(*(payments.prepare_provider_payment(requests[0].request.id,
        f"provider-{i}", configuration_id="merchant") for i in range(10)))
    assert len({r.provider_payment_id for r in reserved}) == 1


async def test_postgres_manual_approval_and_notification_are_claimed_once(postgres_repositories):
    apartments, payments, sessions = postgres_repositories
    apartment = await apartments.upsert_discovered(make_ad())
    request = (await payments.submit(user_id=1, apartment_id=apartment.id,
        username=None, first_name=None, plan=WEEK_PLAN)).request
    await payments.mark_payment_claimed(user_id=1, apartment_id=apartment.id)
    claims = await asyncio.gather(*(payments.claim_admin_notification(request.id, token=f"worker-{i}") for i in range(10)))
    assert claims.count(True) == 1
    results = await asyncio.gather(*(payments.decide(request.id, approve=True, admin_id=999) for _ in range(10)))
    assert results.count("approved") == 1
    assert results.count("already_approved") == 9
    async with sessions() as session:
        assert await session.scalar(select(func.count(PaymentHistory.id))) == 1


async def test_postgres_matching_delivery_is_claimed_once(postgres_repositories):
    apartments, _, sessions = postgres_repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=1, rooms=["1"], districts=[], all_districts=True,
        max_budget=40_000, source="test")
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=-100, message_id=1)
    assert await matching.seed_notifications() == 1
    claims = await asyncio.gather(*(matching.claim_delivery() for _ in range(10)))
    assert len([r for r in claims if r is not None]) == 1

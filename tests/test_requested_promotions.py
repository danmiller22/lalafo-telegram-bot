from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import RequestedCardPromotion, RequestedCardPublication
from app.promotions import promotion_work, publications_in_window
from tests.helpers import make_ad

START = datetime(2026, 10, 14, 0, tzinfo=timezone.utc)

async def work(sessions, now):
    async with sessions() as session:
        return await promotion_work(session, now=now, day_start=START - timedelta(hours=1), day_end=START + timedelta(hours=20))

@pytest.mark.asyncio
async def test_three_total_not_three_repeats_and_gap(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    assert (await work(sessions, START))[0] == []
    assert (await work(sessions, START))[1] == 2
    assert (await work(sessions, START + timedelta(hours=5, minutes=59)))[0] == []
    assert len((await work(sessions, START + timedelta(hours=6)))[0]) == 1
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=START + timedelta(hours=6))
    assert (await work(sessions, START + timedelta(hours=6)))[1] == 1
    await apartments.mark_published(apartment.id, chat_id=1, message_id=3, acknowledged_at=START + timedelta(hours=12))
    assert await work(sessions, START + timedelta(hours=18)) == ([], 0)
    # A new calendar/publication day does not restart the three-send campaign.
    async with sessions() as session:
        assert await promotion_work(session, now=START + timedelta(hours=23), day_start=START + timedelta(hours=22), day_end=START + timedelta(days=2)) == ([], 0)
        rows = await publications_in_window(session, START, START + timedelta(days=1))
        assert len(rows) == 3

@pytest.mark.asyncio
async def test_expiry_blocks_unfinished_campaign(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    async with sessions() as session:
        assert await promotion_work(session, now=START + timedelta(days=1),
            day_start=START + timedelta(hours=22), day_end=START + timedelta(hours=46)) == ([], 0)
        promotion = await session.get(RequestedCardPromotion, apartment.id)
        assert promotion.expires_at.replace(tzinfo=timezone.utc) == START + timedelta(days=1)

@pytest.mark.asyncio
async def test_ack_retry_is_idempotent_and_old_card_resolves(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=START + timedelta(hours=6))
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=START + timedelta(hours=7))
    old = await apartments.get_by_telegram_message(chat_id=1, message_id=1)
    assert old.id == apartment.id
    assert old.published_at.replace(tzinfo=timezone.utc) == START + timedelta(hours=6)
    async with sessions() as session:
        assert len((await session.scalars(select(RequestedCardPublication))).all()) == 2

@pytest.mark.asyncio
async def test_removed_card_and_ordinary_card_do_not_repeat(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1, acknowledged_at=START)
    assert await work(sessions, START + timedelta(hours=6)) == ([], 0)
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=START + timedelta(hours=6), promote_requested=True)
    await apartments.set_availability(apartment.id, status='unavailable', checked_at=START + timedelta(hours=7), reason='source_removed')
    assert await work(sessions, START + timedelta(hours=12)) == ([], 0)

@pytest.mark.asyncio
async def test_new_explicit_request_starts_one_new_day_keeps_history(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    later = START + timedelta(days=2)
    await apartments.mark_published(apartment.id, chat_id=1, message_id=2, acknowledged_at=later, promote_requested=True)
    async with sessions() as session:
        due, reserved = await promotion_work(session, now=later + timedelta(hours=6), day_start=later, day_end=later + timedelta(days=1))
        assert len(due) == 1 and reserved == 2
        assert len(await publications_in_window(session, START, later + timedelta(days=1))) == 2

@pytest.mark.asyncio
async def test_daily_accounting_counts_each_receipt_and_normal_card_once(repositories):
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=12345))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=12346, phone='+996555987654', photo_urls=['https://img5.lalafo.com/other.jpeg']))
    await apartments.mark_published(first.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    await apartments.mark_published(first.id, chat_id=1, message_id=2, acknowledged_at=START + timedelta(hours=6))
    await apartments.mark_published(second.id, chat_id=1, message_id=3, acknowledged_at=START + timedelta(hours=7))
    async with sessions() as session:
        rows = await publications_in_window(session, START, START + timedelta(days=1))
        assert len(rows) == 3
        assert sum(row.apartment_id == first.id for row in rows) == 2

@pytest.mark.asyncio
async def test_day_limit_counts_repeat_receipts_before_claim(repositories):
    from app.inventory import InventoryRepository
    from app.models import ApartmentInventoryQueue
    from app.promotions import record_delivery
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=20001))
    other = await apartments.upsert_discovered(make_ad(lalafo_id=20002, phone='+996555987654', photo_urls=['https://img5.lalafo.com/unique.jpeg']))
    async with sessions.begin() as session:
        for index in range(70):
            await record_delivery(session, first, chat_id=1, message_id=100+index, published_at=START)
        session.add(ApartmentInventoryQueue(apartment_id=other.id, scheduled_at=START, window_key='ordinary', sequence=1, status='queued'))
    assert await InventoryRepository(sessions).claim_due(now=START+timedelta(hours=4)) is None

@pytest.mark.asyncio
async def test_fresh_stock_cannot_consume_reserved_repeat_slots(repositories):
    from app.inventory import InventoryRepository
    from app.models import ApartmentInventoryQueue
    from app.promotions import record_delivery
    apartments, _, sessions = repositories
    history = await apartments.upsert_discovered(make_ad(lalafo_id=20001))
    featured = await apartments.upsert_discovered(make_ad(lalafo_id=20002, phone='+996555987654', photo_urls=['https://img5.lalafo.com/featured.jpeg']))
    fresh = await apartments.upsert_discovered(make_ad(lalafo_id=20003, phone='+996555887654', photo_urls=['https://img5.lalafo.com/fresh.jpeg']))
    await apartments.mark_published(featured.id, chat_id=1, message_id=1, acknowledged_at=START, promote_requested=True)
    async with sessions.begin() as session:
        for index in range(68):
            await record_delivery(session, history, chat_id=1, message_id=100+index, published_at=START)
        session.add(ApartmentInventoryQueue(apartment_id=fresh.id, scheduled_at=START, window_key='ordinary', sequence=1, status='queued'))
    assert (await work(sessions, START+timedelta(hours=4)))[1] == 2
    assert await InventoryRepository(sessions).claim_due(now=START+timedelta(hours=4)) is None

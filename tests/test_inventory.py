from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import random

import pytest
from sqlalchemy import func, select, update

from app.inventory import (
    DISCOVERY_RETRY_MINUTES,
    BISHKEK,
    publication_day_start,
    publication_window_open,
    discovery_period_start,
    InventoryRepository,
    PUBLICATION_SPACING_MINUTES,
    daily_realtor_target,
    daily_publication_target,
    is_central,
    period_publication_targets,
    plan_period,
)
from app.models import Apartment, ApartmentInventoryQueue
from tests.helpers import make_ad


def test_publication_schedule_uses_eighteen_minute_slots() -> None:
    assert PUBLICATION_SPACING_MINUTES == 18


@pytest.mark.asyncio
async def test_photo_delivery_failures_cool_down_without_refill_reset(repositories):
    apartments, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=205001))
    async with sessions.begin() as session:
        row = ApartmentInventoryQueue(apartment_id=apartment.id, scheduled_at=now, window_key="photo-retry", sequence=1, status="publishing", attempts=2)
        session.add(row)
        await session.flush()
        queue_id = row.id
    await inventory.retry_item(queue_id, error="TelegramPublishError")
    async with sessions.begin() as session:
        row = await session.get(ApartmentInventoryQueue, queue_id)
        assert row.status == "queued"
        row.attempts = 3
    await inventory.retry_item(queue_id, error="TelegramPublishError")
    assert await inventory.schedule_period(now=now) == 0
    async with sessions() as session:
        row = await session.get(ApartmentInventoryQueue, queue_id)
        assert row.status == "skipped"
        assert row.attempts == 3
    # A refreshed listing can return after the cooldown; no permanent ban.
    assert await inventory.schedule_period(now=now + timedelta(hours=7)) == 1


@pytest.mark.asyncio
async def test_hosted_discovery_lease_recovers_after_worker_deadline(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = discovery_period_start(datetime.now(timezone.utc)).astimezone(timezone.utc)
    key = await inventory.claim_discovery(now=now, lease_minutes=15)
    assert key is not None
    assert await inventory.claim_discovery(now=now + timedelta(minutes=14)) is None
    assert await inventory.claim_discovery(now=now + timedelta(minutes=16)) == key


def _apartments(count: int, *, central: bool, start_id: int, owner: bool = True):
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    rooms = ("1", "studio")
    return [
        SimpleNamespace(
            id=start_id + index,
            price=25_000 + index % 8 * 1_000,
            rooms=rooms[index % len(rooms)],
            district="Золотой квадрат" if central else "7 мкр",
            source_url=f"https://lalafo.kg/bishkek/ads/test-id-{start_id + index}",
            discovery_priority=False,
            owner_listing=owner,
            seller_type="owner" if owner else "realtor",
            last_seen_at=now,
            updated_at=now,
        )
        for index in range(count)
    ]


def test_two_periods_plan_seventy_cards_from_five_until_two():
    stock = _apartments(220, central=True, start_id=1) + _apartments(
        100, central=False, start_id=300
    )
    for index, item in enumerate(stock):
        item.seller_type = "owner" if index % 2 == 0 else "realtor"
        item.owner_listing = index % 2 == 0
    first_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))
    first = plan_period(stock, period_start=first_start, rng=random.Random(7))
    used = {item.apartment.id for item in first}
    second = plan_period(
        [item for item in stock if item.id not in used],
        period_start=first_start + timedelta(minutes=630),
        rng=random.Random(8),
    )
    all_items = first + second
    daily_target = daily_publication_target(first_start)
    assert daily_target == 70
    assert len(all_items) == daily_target
    assert sum(item.apartment.seller_type == "realtor" for item in all_items) == 49
    assert sum(
        "золотой" in item.apartment.district.casefold() for item in all_items
    ) == round(daily_target * 0.50)
    assert {item.apartment.rooms for item in all_items} == {"1", "studio"}

    times = [item.scheduled_at for item in all_items]
    assert times[0].astimezone(first_start.tzinfo) == first_start
    assert times[-1].astimezone(first_start.tzinfo) == first_start + timedelta(hours=20, minutes=42)
    assert all((after - before) == timedelta(minutes=18) for before, after in zip(times, times[1:]))


def test_period_rotates_confirmed_owners_across_available_districts():
    central = _apartments(40, central=True, start_id=1)
    other = _apartments(60, central=False, start_id=100)
    for index, item in enumerate(other):
        item.district = ("Асанбай", "Джал", "Тунгуч")[index % 3]
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    realtors = _apartments(60, central=True, start_id=300, owner=False)
    planned = plan_period(central + other + realtors, period_start=period_start, rng=random.Random(4))

    assert {item.apartment.district for item in planned} == {
        "Золотой квадрат", "Асанбай", "Джал", "Тунгуч"
    }


def test_period_balances_low_middle_and_high_price_buckets():
    stock = _apartments(96, central=True, start_id=1)
    for index, item in enumerate(stock):
        item.price = (25_000, 30_000, 35_000)[index % 3]
        item.seller_type = "owner" if index % 2 == 0 else "realtor"
        item.owner_listing = index % 2 == 0
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(33))
    buckets = {min(2, (item.apartment.price - 25_000) // 5_000) for item in planned}

    assert buckets == {0, 1, 2}


def test_agent_only_stock_fills_owner_shortage():
    stock = _apartments(100, central=False, start_id=1, owner=False)
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(9))

    assert len(planned) == 35
    assert all(item.apartment.seller_type == "realtor" for item in planned)


def test_period_fills_owner_shortage_with_realtors():
    stock = _apartments(1, central=True, start_id=1) + _apartments(
        100, central=False, start_id=100, owner=False
    )
    period_start = datetime(2026, 9, 13, 15, 30, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(10))

    assert len(planned) == 35
    assert sum("золотой" in item.apartment.district.casefold() for item in planned) == 1
    assert sum(item.apartment.seller_type == "realtor" for item in planned) == 34


def test_central_realtors_can_fill_central_share():
    owners = _apartments(80, central=False, start_id=1)
    realtors = _apartments(80, central=True, start_id=200, owner=False)
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(owners + realtors, period_start=period_start, rng=random.Random(15))

    period_count, central_count = period_publication_targets(period_start)
    assert len(planned) == period_count
    assert any(item.apartment.seller_type == "owner" for item in planned)
    assert sum(is_central(item.apartment.district) for item in planned) == 25
    assert central_count == 18


def test_each_standalone_period_targets_seventy_percent_realtors():
    owners = _apartments(160, central=True, start_id=1)
    realtors = _apartments(80, central=False, start_id=500, owner=False)
    first_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))
    first = plan_period(
        owners + realtors, period_start=first_start, rng=random.Random(21)
    )
    used = {item.apartment.id for item in first}
    second = plan_period(
        [item for item in owners + realtors if item.id not in used],
        period_start=first_start + timedelta(minutes=630),
        rng=random.Random(22),
    )

    assert len(first) == 35
    assert len(second) == 35
    assert sum(item.apartment.seller_type == "realtor" for item in first) == 25
    assert sum(item.apartment.seller_type == "realtor" for item in second) == 24
    assert {item.apartment.seller_type for item in first + second} == {"owner", "realtor"}


def test_unknown_authors_fill_remaining_publication_slots():
    stock = _apartments(100, central=True, start_id=1)
    for item in stock:
        item.owner_listing = False
        item.seller_type = "unknown"
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(13))

    assert len(planned) == 35
    assert all(item.apartment.seller_type == "unknown" for item in planned)


def test_daily_realtor_target_is_seventy_percent():
    start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))
    assert {
        daily_realtor_target(start + timedelta(days=offset)) for offset in range(4)
    } == {49}


def test_period_honors_remaining_daily_realtor_quota():
    owners = _apartments(100, central=True, start_id=1)
    realtors = _apartments(40, central=True, start_id=200, owner=False)
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(
        owners + realtors,
        period_start=period_start,
        non_owner_target=4,
        non_owner_limit=4,
        rng=random.Random(14),
    )

    assert len(planned) == period_publication_targets(period_start)[0]
    assert sum(item.apartment.seller_type == "realtor" for item in planned) == 4


def test_daily_target_is_fixed_at_seventy_cards():
    period_start = datetime(2026, 9, 13, 5, tzinfo=timezone(timedelta(hours=6)))

    assert daily_publication_target(period_start) == daily_publication_target(
        period_start + timedelta(minutes=630)
    )
    assert {
        daily_publication_target(period_start + timedelta(days=offset))
        for offset in range(10)
    } == {70}


@pytest.mark.asyncio
async def test_queue_claims_each_due_apartment_only_once(repositories):
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(
        make_ad(lalafo_id=801, photo_urls=["a", "b"], district="ЦУМ", rooms="1")
    )
    second = await apartments.upsert_discovered(
        make_ad(lalafo_id=802, photo_urls=["c", "d"], district="ЦУМ", rooms="studio")
    )
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add_all(
            [
                ApartmentInventoryQueue(
                    apartment_id=first.id,
                    scheduled_at=now - timedelta(minutes=2),
                    window_key="w",
                    sequence=1,
                ),
                ApartmentInventoryQueue(
                    apartment_id=second.id,
                    scheduled_at=now - timedelta(minutes=1),
                    window_key="w",
                    sequence=2,
                ),
            ]
        )
    inventory = InventoryRepository(sessions)
    claimed = await inventory.claim_due(now=now)
    assert claimed is not None and claimed.apartment_id == first.id
    await inventory.finish_item(claimed.id, status="published")
    assert await inventory.claim_due(now=now) is None
    next_claimed = await inventory.claim_due(
        now=now + timedelta(minutes=PUBLICATION_SPACING_MINUTES + 1)
    )
    assert next_claimed is not None and next_claimed.apartment_id == second.id
    assert (
        await inventory.claim_due(
            now=now + timedelta(minutes=PUBLICATION_SPACING_MINUTES + 1)
        )
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_publishers_cannot_claim_the_same_card(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(
        make_ad(lalafo_id=901, photo_urls=["a", "b"], district="ЦУМ", rooms="1")
    )
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add(
            ApartmentInventoryQueue(
                apartment_id=apartment.id,
                scheduled_at=now - timedelta(minutes=1),
                window_key="concurrent",
                sequence=1,
            )
        )
    inventory = InventoryRepository(sessions)
    claims = await asyncio.gather(
        inventory.claim_due(now=now), inventory.claim_due(now=now)
    )
    assert sum(item is not None for item in claims) == 1


@pytest.mark.asyncio
async def test_schedule_period_removes_two_room_queue_rows(repositories):
    apartments, _, sessions = repositories
    two_room = await apartments.upsert_discovered(
        make_ad(lalafo_id=920, rooms="2", district="ЦУМ")
    )
    one_room = await apartments.upsert_discovered(
        make_ad(lalafo_id=921, rooms="1", district="ЦУМ")
    )
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add_all(
            [
                ApartmentInventoryQueue(
                    apartment_id=two_room.id,
                    scheduled_at=now - timedelta(minutes=2),
                    window_key="room-policy",
                    sequence=1,
                ),
                ApartmentInventoryQueue(
                    apartment_id=one_room.id,
                    scheduled_at=now - timedelta(minutes=1),
                    window_key="room-policy",
                    sequence=2,
                ),
            ]
        )

    await InventoryRepository(sessions).schedule_period(now=now)

    async with sessions() as session:
        deleted = await session.scalar(
            select(func.count())
            .select_from(ApartmentInventoryQueue)
            .where(ApartmentInventoryQueue.apartment_id == two_room.id)
        )
        retained = await session.scalar(
            select(func.count())
            .select_from(ApartmentInventoryQueue)
            .where(ApartmentInventoryQueue.apartment_id == one_room.id)
        )
    assert deleted == 0
    assert retained == 1


@pytest.mark.asyncio
async def test_afternoon_schedule_fills_only_remaining_period_target(repositories):
    apartments, _, sessions = repositories
    stored = [
        await apartments.upsert_discovered(
            make_ad(lalafo_id=10_000 + index, district="ЦУМ", rooms="1", owner_listing=index % 2 == 0)
        )
        for index in range(90)
    ]
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    async with sessions.begin() as session:
        await session.execute(
            update(Apartment)
            .where(Apartment.id.in_([item.id for item in stored[:6]]))
            .values(publication_status="published", published_at=now - timedelta(minutes=20))
        )

    queued = await InventoryRepository(sessions).schedule_period(
        now=now, rng=random.Random(19)
    )

    assert queued == period_publication_targets(now)[0] - 6
    async with sessions() as session:
        first = await session.scalar(
            select(ApartmentInventoryQueue)
            .order_by(ApartmentInventoryQueue.scheduled_at.asc())
            .limit(1)
        )
    assert first is not None
    assert first.scheduled_at <= now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_late_period_schedule_keeps_three_to_five_per_hour_cadence(repositories):
    apartments, _, sessions = repositories
    for index in range(40):
        await apartments.upsert_discovered(
            make_ad(lalafo_id=20_000 + index, district="ЦУМ", rooms="1", owner_listing=index % 2 == 0)
        )
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

    queued = await InventoryRepository(sessions).schedule_period(
        now=now, rng=random.Random(23)
    )

    assert queued == 34
    async with sessions() as session:
        scheduled = list(
            (
                await session.scalars(
                    select(ApartmentInventoryQueue.scheduled_at).order_by(
                        ApartmentInventoryQueue.scheduled_at.asc()
                    )
                )
            ).all()
        )
    deltas = [
        round((after - before).total_seconds() / 60)
        for before, after in zip(scheduled, scheduled[1:])
    ]
    assert len(deltas) == 33
    assert all(delta == 18 for delta in deltas)

    assert (
        await InventoryRepository(sessions).schedule_period(
            now=now + timedelta(minutes=10), rng=random.Random(24)
        )
        == 0
    )
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ApartmentInventoryQueue)
            )
            == 34
        )


@pytest.mark.asyncio
async def test_claim_due_prioritizes_central_card_for_daily_share(repositories):
    apartments, _, sessions = repositories
    outskirts = await apartments.upsert_discovered(
        make_ad(lalafo_id=930, rooms="1", district="Асанбай")
    )
    central = await apartments.upsert_discovered(
        make_ad(lalafo_id=931, rooms="studio", district="ЦУМ")
    )
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        session.add_all(
            [
                ApartmentInventoryQueue(
                    apartment_id=outskirts.id,
                    scheduled_at=now - timedelta(minutes=2),
                    window_key="central-share",
                    sequence=1,
                ),
                ApartmentInventoryQueue(
                    apartment_id=central.id,
                    scheduled_at=now - timedelta(minutes=1),
                    window_key="central-share",
                    sequence=2,
                ),
            ]
        )

    claimed = await InventoryRepository(sessions).claim_due(now=now)

    assert claimed is not None and claimed.apartment_id == central.id


@pytest.mark.asyncio
async def test_claim_due_accepts_queued_agents(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(5):
        apartment = await apartments.upsert_discovered(
            make_ad(
                lalafo_id=950 + index,
                rooms="1",
                seller_type="realtor",
                owner_listing=False,
            )
        )
        rows.append(apartment)
    async with sessions.begin() as session:
        session.add_all(
            [
                ApartmentInventoryQueue(
                    apartment_id=apartment.id,
                    scheduled_at=now - timedelta(minutes=1),
                    window_key="realtor-cap",
                    sequence=index,
                )
                for index, apartment in enumerate(rows, start=1)
            ]
        )

    inventory = InventoryRepository(sessions)
    assert await inventory.claim_due(now=now) is not None
    async with sessions() as session:
        skipped = await session.scalar(
            select(func.count())
            .select_from(ApartmentInventoryQueue)
            .where(
                ApartmentInventoryQueue.status == "skipped",
                ApartmentInventoryQueue.last_error == "not_confirmed_owner",
            )
        )
    assert skipped == 0


@pytest.mark.asyncio
async def test_schedule_uses_only_daily_realtor_allowance(repositories):
    apartments, _, sessions = repositories
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    for index in range(40):
        await apartments.upsert_discovered(
            make_ad(lalafo_id=30_000 + index, seller_type="owner", owner_listing=True)
        )
    for index in range(10):
        await apartments.upsert_discovered(
            make_ad(
                lalafo_id=31_000 + index,
                seller_type="realtor",
                owner_listing=False,
            )
        )

    await InventoryRepository(sessions).schedule_period(now=now, rng=random.Random(51))

    async with sessions() as session:
        realtor_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ApartmentInventoryQueue)
                .join(Apartment)
                .where(Apartment.seller_type == "realtor")
            )
            or 0
        )
        unknown_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ApartmentInventoryQueue)
                .join(Apartment)
                .where(Apartment.seller_type == "unknown")
            )
            or 0
        )
    assert realtor_count == min(10, daily_realtor_target(now))
    assert unknown_count == 0


@pytest.mark.asyncio
async def test_schedule_repeats_only_after_forty_eight_hours(repositories):
    apartments, _, sessions = repositories
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    eligible = await apartments.upsert_discovered(make_ad(lalafo_id=32_001))
    too_new = await apartments.upsert_discovered(make_ad(lalafo_id=32_002))
    await apartments.upsert_discovered(make_ad(lalafo_id=32_003, owner_listing=False))
    async with sessions.begin() as session:
        await session.execute(
            update(Apartment)
            .where(Apartment.id == eligible.id)
            .values(
                publication_status="published",
                published_at=now - timedelta(hours=49),
                last_seen_at=now,
            )
        )
        await session.execute(
            update(Apartment)
            .where(Apartment.id == too_new.id)
            .values(
                publication_status="published",
                published_at=now - timedelta(hours=47),
                last_seen_at=now,
            )
        )

    await InventoryRepository(sessions).schedule_period(now=now, rng=random.Random(52))

    async with sessions() as session:
        queued_ids = set(
            (
                await session.scalars(
                    select(ApartmentInventoryQueue.apartment_id)
                )
            ).all()
        )
    assert eligible.id in queued_ids
    assert too_new.id not in queued_ids


@pytest.mark.asyncio
async def test_claim_due_skips_old_unsupported_source_even_if_owner(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    apartment = await apartments.upsert_discovered(
        make_ad(lalafo_id=-7999999999999999999, source_url="https://joyka.kg/test")
    )
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(
            apartment_id=apartment.id,
            scheduled_at=now - timedelta(minutes=1),
            window_key="old-source",
            sequence=1,
        ))

    assert await InventoryRepository(sessions).claim_due(now=now) is None
    async with sessions() as session:
        row = (await session.scalars(select(ApartmentInventoryQueue))).one()
    assert row.status == "skipped"


@pytest.mark.asyncio
async def test_failed_empty_discovery_retries_after_cooldown(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)
    key = await inventory.claim_discovery(now=now)
    assert key is not None
    await inventory.finish_discovery(
        key,
        success=False,
        discovered=0,
        queued=0,
        error="EmptyInventory",
    )

    assert (
        await inventory.claim_discovery(
            now=now + timedelta(minutes=DISCOVERY_RETRY_MINUTES - 1)
        )
        is None
    )
    assert (
        await inventory.claim_discovery(
            now=now + timedelta(minutes=DISCOVERY_RETRY_MINUTES + 1)
        )
        == key
    )


@pytest.mark.asyncio
async def test_thin_successful_discovery_is_rebuilt(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)
    key = await inventory.claim_discovery(now=now)
    assert key is not None
    await inventory.finish_discovery(
        key,
        success=True,
        discovered=6,
        queued=6,
    )

    assert await inventory.claim_discovery(now=now) == key


@pytest.mark.asyncio
@pytest.mark.parametrize("stock_count,should_refill", [(12, True), (35, False)])
async def test_discovery_refills_a_twelve_card_queue_until_full_period(repositories, stock_count, should_refill):
    apartments, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = discovery_period_start(datetime.now(timezone.utc)).astimezone(timezone.utc)
    key = await inventory.claim_discovery(now=now)
    for index in range(stock_count):
        await apartments.upsert_discovered(make_ad(lalafo_id=110000 + index, phone=f"+996555{100000 + index}"))
    assert await inventory.schedule_period(now=now) == stock_count
    await inventory.finish_discovery(key, success=True, discovered=stock_count, queued=stock_count)
    claimed = await inventory.claim_discovery(now=now)
    assert (claimed is not None) == should_refill


@pytest.mark.asyncio
async def test_full_period_does_not_refill_cards_already_published(repositories):
    apartments, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = discovery_period_start().astimezone(timezone.utc) + timedelta(minutes=1)
    key = await inventory.claim_discovery(now=now)
    for index in range(35):
        apartment = await apartments.upsert_discovered(make_ad(lalafo_id=120000 + index, phone=f"+996555{200000 + index}"))
        if index < 25:
            await apartments.mark_published(apartment.id, chat_id=-1001, message_id=1000 + index)
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.publication_status == "published").values(published_at=now))
    assert await inventory.schedule_period(now=now) == 10
    assert await inventory.period_published_count(now=now) == 25
    await inventory.finish_discovery(key, success=True, discovered=35, queued=10)
    assert await inventory.claim_discovery(now=now) is None


@pytest.mark.asyncio
async def test_code_change_rebuilds_queue_without_erasing_repost_history(repositories):
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=88001))
    await apartments.mark_published(apartment.id, chat_id=-1001, message_id=501)
    async with sessions.begin() as session:
        session.add(
            ApartmentInventoryQueue(
                apartment_id=apartment.id,
                scheduled_at=datetime.now(timezone.utc),
                window_key="old-code",
                sequence=1,
            )
        )

    inventory = InventoryRepository(sessions)
    assert await inventory.reset_publication_history_for_code("commit-one") is True
    refreshed = await apartments.get(apartment.id)
    assert refreshed.publication_status == "published"
    assert refreshed.telegram_chat_id == -1001
    assert refreshed.telegram_message_id == 501
    assert refreshed.published_at is not None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue)) == 0

    assert await inventory.reset_publication_history_for_code("commit-one") is False


@pytest.mark.asyncio
async def test_code_change_keeps_five_minute_publication_cooldown(repositories):
    apartments, _, sessions = repositories
    published = await apartments.upsert_discovered(make_ad(lalafo_id=88011))
    queued = await apartments.upsert_discovered(make_ad(lalafo_id=88012))
    published = await apartments.mark_published(
        published.id, chat_id=-1001, message_id=511
    )
    assert published.published_at is not None
    published_at = published.published_at

    inventory = InventoryRepository(sessions)
    assert await inventory.reset_publication_history_for_code("commit-cooldown") is True
    async with sessions.begin() as session:
        session.add(
            ApartmentInventoryQueue(
                apartment_id=queued.id,
                scheduled_at=published_at,
                window_key="new-code",
                sequence=1,
            )
        )

    assert (
        await inventory.claim_due(now=published_at + timedelta(minutes=1)) is None
    )
    assert (
        await inventory.claim_due(
            now=published_at + timedelta(minutes=PUBLICATION_SPACING_MINUTES)
        )
        is not None
    )


@pytest.mark.asyncio
async def test_availability_sweep_is_claimed_twice_daily(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)

    assert await inventory.claim_availability_sweep(now=now) is True
    assert await inventory.claim_availability_sweep(now=now + timedelta(hours=11)) is False
    assert await inventory.claim_availability_sweep(now=now + timedelta(hours=12)) is True


@pytest.mark.asyncio
async def test_claim_prioritizes_underrepresented_seller(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    owner = await apartments.upsert_discovered(make_ad(lalafo_id=99101, district="ЦУМ"))
    realtor = await apartments.upsert_discovered(make_ad(lalafo_id=99102, owner_listing=False))
    async with sessions.begin() as session:
        for index, apartment in enumerate((owner, realtor)):
            session.add(ApartmentInventoryQueue(
                apartment_id=apartment.id, scheduled_at=now - timedelta(minutes=1),
                window_key="balanced", sequence=index + 1,
            ))
    inventory = InventoryRepository(sessions)
    claimed = await inventory.claim_due(now=now)
    assert claimed.apartment_id == realtor.id
    await apartments.mark_published(realtor.id, chat_id=-1001, message_id=99201)
    await inventory.finish_item(claimed.id, status="published")
    claimed = await inventory.claim_due(now=now + timedelta(minutes=19))
    assert claimed.apartment_id == owner.id


@pytest.mark.asyncio
async def test_seller_policy_rebuilds_queue_without_git_environment(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    assert await inventory.reset_publication_history_for_code("") is True
    assert await inventory.reset_publication_history_for_code("") is False


@pytest.mark.asyncio
async def test_stock_load_reserves_room_for_both_seller_categories(repositories, monkeypatch):
    monkeypatch.setattr("app.inventory.MAX_FRESH_STOCK_LOAD", 4)
    apartments, _, sessions = repositories
    for index in range(10):
        await apartments.upsert_discovered(make_ad(
            lalafo_id=99300 + index, owner_listing=index < 6,
        ))
    inventory = InventoryRepository(sessions)
    assert await inventory.schedule_period(now=datetime.now(timezone.utc)) == 4
    async with sessions() as session:
        kinds = (await session.scalars(
            select(Apartment.seller_type).join(ApartmentInventoryQueue)
        )).all()
    assert kinds.count("owner") == 1
    assert kinds.count("realtor") == 3


@pytest.mark.asyncio
async def test_saved_contact_verification_reaches_public_card(repositories):
    from scripts.scrape_publish import apartment_to_ad
    from app.telegram.formatting import format_public_apartment
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(phone_source_version=2))
    ad = apartment_to_ad(apartment)
    assert ad.phone_source_version == 2
    assert "Собственник" not in format_public_apartment(ad, bot_username="rentttkg")
    assert "Контакты проверены" not in format_public_apartment(ad, bot_username="rentttkg")


def test_owner_only_stock_fills_realtor_shortage():
    stock = _apartments(100, central=True, start_id=1)
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    planned = plan_period(stock, period_start=start)
    assert len(planned) == 35
    assert all(item.apartment.seller_type == "owner" for item in planned)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_stock_load_uses_spare_category_capacity(repositories, monkeypatch, owner):
    monkeypatch.setattr("app.inventory.MAX_FRESH_STOCK_LOAD", 4)
    apartments, _, sessions = repositories
    for index in range(6):
        await apartments.upsert_discovered(make_ad(lalafo_id=99500 + index, owner_listing=owner))
    assert await InventoryRepository(sessions).schedule_period(now=datetime.now(timezone.utc)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_claim_can_exceed_category_share_to_fill_shortage(repositories, owner):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    past = []
    for index in range(48):
        past.append(await apartments.upsert_discovered(make_ad(
            lalafo_id=99600 + index, owner_listing=owner,
        )))
    waiting = await apartments.upsert_discovered(make_ad(lalafo_id=99700, owner_listing=owner))
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(
            Apartment.id.in_([item.id for item in past])
        ).values(publication_status="published", published_at=now - timedelta(minutes=19)))
        session.add(ApartmentInventoryQueue(
            apartment_id=waiting.id, scheduled_at=now - timedelta(minutes=1),
            window_key="category-refill", sequence=1,
        ))
    claimed = await InventoryRepository(sessions).claim_due(now=now)
    assert claimed is not None and claimed.apartment_id == waiting.id


@pytest.mark.asyncio
async def test_unknown_author_can_be_scheduled_and_published(repositories, monkeypatch):
    monkeypatch.setattr("app.inventory.MAX_FRESH_STOCK_LOAD", 4)
    apartments, _, sessions = repositories
    for index in range(6):
        await apartments.upsert_discovered(make_ad(
            lalafo_id=99800 + index, seller_type="unknown", owner_listing=False,
        ))
    now = datetime.now(timezone.utc)
    inventory = InventoryRepository(sessions)
    assert await inventory.schedule_period(now=now) == 4
    claimed = await inventory.claim_due(now=now)
    assert claimed is not None
    assert claimed.apartment.seller_type == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("stock_size", [1, 2, 6, 11])
async def test_thin_stock_is_due_now_then_every_fifteen_minutes(repositories, stock_size):
    apartments, _, sessions = repositories
    now = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
    for index in range(stock_size):
        await apartments.upsert_discovered(make_ad(lalafo_id=99900 + index))
    assert await InventoryRepository(sessions).schedule_period(now=now, rng=random.Random(5)) == stock_size
    async with sessions() as session:
        times = list((await session.scalars(select(ApartmentInventoryQueue.scheduled_at)
            .order_by(ApartmentInventoryQueue.scheduled_at))).all())
    assert times[0].replace(tzinfo=timezone.utc) <= now
    assert all((after - before).total_seconds() == 1080 for before, after in zip(times, times[1:]))


@pytest.mark.parametrize("hour,minute,expected", [(1,59,True),(2,0,False),(4,59,False),(5,0,True),(23,59,True)])
def test_publication_window_boundaries(hour, minute, expected):
    now = datetime(2026,10,7,hour,minute,tzinfo=BISHKEK)
    assert publication_window_open(now) is expected


def test_midnight_keeps_previous_publication_day_and_period():
    now = datetime(2026,10,8,1,0,tzinfo=BISHKEK)
    assert publication_day_start(now) == datetime(2026,10,7,5,tzinfo=BISHKEK)
    assert discovery_period_start(now) == datetime(2026,10,7,15,30,tzinfo=BISHKEK)
    assert discovery_period_start(now.replace(hour=3)) == datetime(2026,10,8,5,tzinfo=BISHKEK)


@pytest.mark.asyncio
async def test_quiet_hours_never_claim_overdue_cards(repositories):
    apartments, _, sessions = repositories
    item = await apartments.upsert_discovered(make_ad(lalafo_id=200001))
    now = datetime(2026,10,7,3,tzinfo=BISHKEK)
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=item.id, scheduled_at=now-timedelta(hours=4), window_key="overdue", sequence=1))
    inventory = InventoryRepository(sessions)
    assert await inventory.claim_due(now=now) is None
    assert await inventory.claim_due(now=now.replace(hour=5)) is not None


@pytest.mark.asyncio
async def test_late_refill_never_schedules_into_quiet_hours(repositories):
    apartments, _, sessions = repositories
    for index in range(8):
        await apartments.upsert_discovered(make_ad(lalafo_id=201000+index))
    now = datetime(2026,10,8,1,50,tzinfo=BISHKEK)
    assert await InventoryRepository(sessions).schedule_period(now=now) == 1
    async with sessions() as session:
        scheduled = await session.scalar(select(ApartmentInventoryQueue.scheduled_at))
    assert scheduled < now.replace(hour=2,minute=0).astimezone(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_midnight_does_not_reset_seventy_card_limit(repositories):
    apartments, _, sessions = repositories
    now = datetime(2026,10,8,0,30,tzinfo=BISHKEK).astimezone(timezone.utc)
    ids = []
    for index in range(70):
        apartment = await apartments.upsert_discovered(make_ad(lalafo_id=202000+index, phone=f"+996555{300000+index}"))
        ids.append(apartment.id)
    waiting = await apartments.upsert_discovered(make_ad(lalafo_id=203000, phone="+996555400000"))
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id.in_(ids)).values(publication_status="published", published_at=now-timedelta(hours=1)))
        session.add(ApartmentInventoryQueue(apartment_id=waiting.id, scheduled_at=now-timedelta(minutes=1), window_key="overdue", sequence=1))
    inventory = InventoryRepository(sessions)
    assert await inventory.claim_due(now=now) is None
    assert await inventory.claim_due(now=datetime(2026,10,8,5,tzinfo=BISHKEK)) is not None


@pytest.mark.asyncio
async def test_watermarked_listing_is_not_queued_again(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    item = await apartments.upsert_discovered(make_ad(lalafo_id=204000))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=item.id, scheduled_at=now, window_key="rejected", sequence=1, status="skipped", last_error="watermarked_photos"))
    assert await InventoryRepository(sessions).schedule_period(now=now) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("overdue_count,expected", [(1,False),(3,True)])
async def test_backlog_catches_up_without_waiting_eighteen_minutes(repositories, overdue_count, expected):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc).astimezone(BISHKEK).replace(hour=5,minute=30).astimezone(timezone.utc)
    previous = await apartments.upsert_discovered(make_ad(lalafo_id=220000, phone="+996555500000"))
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id==previous.id).values(publication_status="published", published_at=now-timedelta(minutes=6)))
    for index in range(overdue_count):
        item = await apartments.upsert_discovered(make_ad(lalafo_id=221000+index,phone=f"+996555{501000+index}"))
        async with sessions.begin() as session:
            session.add(ApartmentInventoryQueue(apartment_id=item.id,scheduled_at=now-timedelta(minutes=30),window_key="backlog",sequence=index))
    claimed = await InventoryRepository(sessions).claim_due(now=now)
    assert (claimed is not None) is expected


@pytest.mark.asyncio
async def test_failed_photo_respects_backoff_even_with_lookahead(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    item = await apartments.upsert_discovered(make_ad(lalafo_id=222000))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(apartment_id=item.id,scheduled_at=now+timedelta(minutes=3),window_key="retry",sequence=1,last_error="photo_inspection_failed"))
    inventory = InventoryRepository(sessions)
    assert await inventory.claim_due(now=now, eligible_until=now+timedelta(minutes=10)) is None
    assert await inventory.claim_due(now=now+timedelta(minutes=4)) is not None


@pytest.mark.asyncio
async def test_late_day_catchup_can_use_future_reserved_stock(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc).astimezone(BISHKEK).replace(hour=22,minute=0).astimezone(timezone.utc)
    old=await apartments.upsert_discovered(make_ad(lalafo_id=223000,phone="+996555601000"))
    waiting=await apartments.upsert_discovered(make_ad(lalafo_id=223001,phone="+996555601001"))
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id==old.id).values(publication_status="published",published_at=now-timedelta(minutes=6)))
        session.add(ApartmentInventoryQueue(apartment_id=waiting.id,scheduled_at=now+timedelta(minutes=18),window_key="today",sequence=1))
    assert await InventoryRepository(sessions).claim_due(now=now) is not None


@pytest.mark.asyncio
async def test_claim_mixes_price_ranges_after_a_low_price_publication(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    previous = await apartments.upsert_discovered(make_ad(lalafo_id=280000, price=25_000, district="ЦУМ", phone="+996555280000"))
    await apartments.mark_published(previous.id, chat_id=-1001, message_id=1)
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id==previous.id).values(published_at=now-timedelta(minutes=20)))
    for index,price in enumerate((25_000,33_000,39_000)):
        card=await apartments.upsert_discovered(make_ad(lalafo_id=280001+index,price=price,district="ЦУМ",phone=f"+99655528000{index+1}"))
        async with sessions.begin() as session:
            session.add(ApartmentInventoryQueue(apartment_id=card.id,scheduled_at=now-timedelta(minutes=3-index),window_key="mixed-prices",sequence=index))
    claimed=await InventoryRepository(sessions).claim_due(now=now)
    assert claimed is not None
    assert claimed.apartment.price in {33_000,39_000}

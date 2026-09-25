from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import random

import pytest
from sqlalchemy import func, select, update

from app.inventory import (
    DISCOVERY_RETRY_MINUTES,
    InventoryRepository,
    PUBLICATION_SPACING_MINUTES,
    daily_publication_target,
    period_publication_targets,
    plan_period,
)
from app.models import Apartment, ApartmentInventoryQueue
from tests.helpers import make_ad


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


def test_two_periods_plan_random_50_to_60_card_day():
    stock = _apartments(220, central=True, start_id=1) + _apartments(
        100, central=False, start_id=300
    )
    first_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))
    first = plan_period(stock, period_start=first_start, rng=random.Random(7))
    used = {item.apartment.id for item in first}
    second = plan_period(
        [item for item in stock if item.id not in used],
        period_start=first_start + timedelta(hours=12),
        rng=random.Random(8),
    )
    all_items = first + second
    daily_target = daily_publication_target(first_start)
    assert 50 <= daily_target <= 60
    assert len(all_items) == daily_target
    assert sum(
        "золотой" in item.apartment.district.casefold() for item in all_items
    ) == round(daily_target * 0.50)
    assert {item.apartment.rooms for item in all_items} == {"1", "studio"}

    first_local = [item.scheduled_at.astimezone(first_start.tzinfo) for item in first]
    second_local = [item.scheduled_at.astimezone(first_start.tzinfo) for item in second]
    assert all(5 <= value.hour < 15 for value in first_local)
    assert all(
        value.hour > 14 or (value.hour == 14 and value.minute >= 25)
        for value in second_local
    )
    assert all(value.date() == first_start.date() for value in first_local + second_local)
    for period_items in (first, second):
        local_times = [
            item.scheduled_at.astimezone(first_start.tzinfo)
            for item in period_items
        ]
        batch_hours = {value.hour for value in local_times if value.minute == 0}
        expected_hours = (
            {5, 7, 9, 11, 13}
            if period_items is first
            else {15, 17, 19, 21, 23}
        )
        assert batch_hours == expected_hours
        for hour in expected_hours:
            batch = [value for value in local_times if value.hour == hour]
            assert 5 <= len(batch) <= 6
            assert all(
                round((after - before).total_seconds())
                == PUBLICATION_SPACING_MINUTES * 60
                for before, after in zip(batch, batch[1:])
            )


def test_period_rotates_confirmed_owners_across_available_districts():
    central = _apartments(40, central=True, start_id=1)
    other = _apartments(60, central=False, start_id=100)
    for index, item in enumerate(other):
        item.district = ("Асанбай", "Джал", "Тунгуч")[index % 3]
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(central + other, period_start=period_start, rng=random.Random(4))

    assert {item.apartment.district for item in planned} == {
        "Золотой квадрат", "Асанбай", "Джал", "Тунгуч"
    }


def test_period_accepts_agent_stock():
    stock = _apartments(100, central=False, start_id=1, owner=False)
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(9))

    assert len(planned) == 5
    assert all(item.apartment.seller_type == "realtor" for item in planned)


def test_period_fills_from_all_author_types():
    stock = _apartments(1, central=True, start_id=1) + _apartments(
        100, central=False, start_id=100, owner=False
    )
    period_start = datetime(2026, 9, 13, 12, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(10))

    assert len(planned) == 6
    assert sum("золотой" in item.apartment.district.casefold() for item in planned) == 1


def test_central_realtors_can_fill_central_share():
    owners = _apartments(80, central=False, start_id=1)
    realtors = _apartments(80, central=True, start_id=200, owner=False)
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(owners + realtors, period_start=period_start, rng=random.Random(15))

    period_count, _ = period_publication_targets(period_start)
    assert len(planned) == period_count
    assert any(item.apartment.seller_type == "owner" for item in planned)
    assert sum(item.apartment.seller_type == "realtor" for item in planned) == 5


def test_two_periods_place_ten_agents_at_random_positions():
    owners = _apartments(160, central=True, start_id=1)
    realtors = _apartments(80, central=False, start_id=500, owner=False)
    first_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))
    first = plan_period(
        owners + realtors, period_start=first_start, rng=random.Random(21)
    )
    used = {item.apartment.id for item in first}
    second = plan_period(
        [item for item in owners + realtors if item.id not in used],
        period_start=first_start + timedelta(hours=12),
        rng=random.Random(22),
    )

    assert sum(
        item.apartment.seller_type == "realtor" for item in first + second
    ) == 10
    first_positions = [
        item.sequence for item in first if item.apartment.seller_type == "realtor"
    ]
    second_positions = [
        item.sequence for item in second if item.apartment.seller_type == "realtor"
    ]
    assert first_positions != second_positions


def test_unknown_authors_are_included():
    stock = _apartments(100, central=True, start_id=1)
    for item in stock:
        item.owner_listing = False
        item.seller_type = "unknown"
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(13))

    assert len(planned) == period_publication_targets(period_start)[0]
    assert all(item.apartment.seller_type == "unknown" for item in planned)


def test_period_does_not_cap_non_owners():
    owners = _apartments(100, central=True, start_id=1)
    realtors = _apartments(40, central=True, start_id=200, owner=False)
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(
        owners + realtors,
        period_start=period_start,
        non_owner_target=4,
        non_owner_limit=4,
        rng=random.Random(14),
    )

    assert len(planned) == period_publication_targets(period_start)[0]
    assert all(item.apartment.seller_type in {"owner", "realtor", "unknown"} for item in planned)


def test_daily_target_is_stable_for_retries_but_changes_across_dates():
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    assert daily_publication_target(period_start) == daily_publication_target(
        period_start + timedelta(hours=12)
    )
    targets = {
        daily_publication_target(period_start + timedelta(days=offset))
        for offset in range(10)
    }
    assert targets.issubset(set(range(50, 61)))
    assert len(targets) > 1


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
    next_claimed = await inventory.claim_due(now=now)
    assert next_claimed is not None and next_claimed.apartment_id == second.id
    assert await inventory.claim_due(now=now) is None


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
async def test_schedule_period_deletes_old_two_room_queue_rows(repositories):
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
async def test_afternoon_schedule_fills_remaining_daily_target_today(repositories):
    apartments, _, sessions = repositories
    stored = [
        await apartments.upsert_discovered(
            make_ad(lalafo_id=10_000 + index, district="ЦУМ", rooms="1")
        )
        for index in range(90)
    ]
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    async with sessions.begin() as session:
        await session.execute(
            update(Apartment)
            .where(Apartment.id.in_([item.id for item in stored[:18]]))
            .values(publication_status="published", published_at=now - timedelta(hours=1))
        )

    queued = await InventoryRepository(sessions).schedule_period(
        now=now, rng=random.Random(19)
    )

    assert queued == daily_publication_target(now) - 18
    async with sessions() as session:
        first = await session.scalar(
            select(ApartmentInventoryQueue)
            .order_by(ApartmentInventoryQueue.scheduled_at.asc())
            .limit(1)
        )
    assert first is not None
    assert first.scheduled_at <= now.replace(tzinfo=None)


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
async def test_code_change_resets_dedupe_without_touching_old_message(repositories):
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
    assert refreshed.publication_status == "discovered"
    assert refreshed.telegram_chat_id == -1001
    assert refreshed.telegram_message_id == 501
    assert refreshed.published_at is not None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ApartmentInventoryQueue)) == 0

    assert await inventory.reset_publication_history_for_code("commit-one") is False


@pytest.mark.asyncio
async def test_availability_sweep_is_claimed_twice_daily(repositories):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)

    assert await inventory.claim_availability_sweep(now=now) is True
    assert await inventory.claim_availability_sweep(now=now + timedelta(hours=11)) is False
    assert await inventory.claim_availability_sweep(now=now + timedelta(hours=12)) is True

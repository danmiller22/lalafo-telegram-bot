from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import random

import pytest

from app.inventory import DISCOVERY_RETRY_MINUTES, InventoryRepository, plan_period
from app.models import ApartmentInventoryQueue
from tests.helpers import make_ad


def _apartments(count: int, *, central: bool, start_id: int, owner: bool = True):
    from types import SimpleNamespace

    now = datetime.now(timezone.utc)
    rooms = ("studio", "1", "2")
    return [
        SimpleNamespace(
            id=start_id + index,
            price=25_000 + index % 8 * 1_000,
            rooms=rooms[index % len(rooms)],
            district="Золотой квадрат" if central else "7 мкр",
            discovery_priority=False,
            owner_listing=owner,
            last_seen_at=now,
            updated_at=now,
        )
        for index in range(count)
    ]


def test_two_periods_plan_35_cards_across_every_hour():
    stock = _apartments(60, central=True, start_id=1) + _apartments(
        40, central=False, start_id=100
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
    assert len(all_items) == 35
    assert sum("золотой" in item.apartment.district.casefold() for item in all_items) == 35
    assert 5 <= sum(item.apartment.rooms == "2" for item in all_items) <= 10

    for planned in (first, second):
        windows = {}
        for item in planned:
            windows.setdefault(item.window_key, []).append(item)
        assert len(windows) == 12
        assert all(len(items) in {1, 2} for items in windows.values())
        starts = []
        for items in windows.values():
            ordered = sorted(items, key=lambda item: item.sequence)
            starts.append(ordered[0].scheduled_at)
            central_count = sum(
                "золотой" in item.apartment.district.casefold() for item in items
            )
            assert central_count == len(items)
            assert sum(item.apartment.rooms == "2" for item in items) <= 1
            for before, after in zip(ordered, ordered[1:]):
                assert (
                    timedelta(minutes=8)
                    <= after.scheduled_at - before.scheduled_at
                    <= timedelta(minutes=15)
                )
        for before, after in zip(sorted(starts), sorted(starts)[1:]):
            assert timedelta(minutes=50) <= after - before <= timedelta(minutes=70)


def test_period_uses_broader_stock_when_no_central_apartments_exist():
    stock = _apartments(24, central=False, start_id=1, owner=False)
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(9))

    assert len(planned) == 18
    assert all(not "золотой" in item.apartment.district.casefold() for item in planned)
    assert 5 <= sum(item.apartment.rooms == "2" for item in planned) <= 10


def test_period_keeps_single_central_card_and_fills_with_realtors():
    stock = _apartments(1, central=True, start_id=1) + _apartments(
        30, central=False, start_id=100, owner=False
    )
    period_start = datetime(2026, 9, 13, 12, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(stock, period_start=period_start, rng=random.Random(10))

    assert len(planned) == 17
    assert sum("золотой" in item.apartment.district.casefold() for item in planned) == 1


def test_period_spreads_random_reposts_across_separate_windows():
    fresh = _apartments(40, central=True, start_id=1)
    repeats = _apartments(8, central=True, start_id=100)
    for item in repeats:
        item.rooms = "1"
    repeat_ids = {item.id for item in repeats}
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(
        fresh + repeats,
        period_start=period_start,
        repeat_apartment_ids=repeat_ids,
        rng=random.Random(11),
    )

    planned_repeats = [
        item for item in planned if item.apartment.id in repeat_ids
    ]
    assert len(planned_repeats) == 8
    assert len({item.window_key for item in planned_repeats}) == 8


def test_hourly_period_can_be_filled_from_48_hour_reposts():
    repeats = _apartments(24, central=True, start_id=500)
    for item in repeats:
        item.rooms = "1"
    period_start = datetime(2026, 9, 13, 0, tzinfo=timezone(timedelta(hours=6)))

    planned = plan_period(
        repeats,
        period_start=period_start,
        repeat_apartment_ids={item.id for item in repeats},
        rng=random.Random(12),
    )

    assert len(planned) == 18
    assert len({item.window_key for item in planned}) == 12


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

    assert await inventory.claim_discovery(now=now + timedelta(minutes=5)) is None
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

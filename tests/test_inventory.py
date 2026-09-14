from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import random

import pytest

from app.inventory import InventoryRepository, plan_period
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


def test_two_periods_plan_36_cards_with_32_central_and_fewer_two_bedrooms():
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
    assert len(all_items) == 36
    assert sum("золотой" in item.apartment.district.casefold() for item in all_items) == 32
    assert 5 <= sum(item.apartment.rooms == "2" for item in all_items) <= 10

    for planned in (first, second):
        windows = {}
        for item in planned:
            windows.setdefault(item.window_key, []).append(item)
        assert sorted(len(items) for items in windows.values()) == [3, 3, 4, 4, 4]
        starts = []
        for items in windows.values():
            ordered = sorted(items, key=lambda item: item.sequence)
            starts.append(ordered[0].scheduled_at)
            central_count = sum(
                "золотой" in item.apartment.district.casefold() for item in items
            )
            assert central_count == len(items) if len(items) == 3 else central_count >= 3
            assert sum(item.apartment.rooms == "2" for item in items) <= 1
            for before, after in zip(ordered, ordered[1:]):
                assert timedelta(minutes=8) <= after.scheduled_at - before.scheduled_at <= timedelta(minutes=15)
        for before, after in zip(sorted(starts), sorted(starts)[1:]):
            assert after - before >= timedelta(minutes=90)


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

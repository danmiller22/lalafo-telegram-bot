from __future__ import annotations

from datetime import date

import pytest

from app.featured.repository import FeaturedRepository
from app.models import Apartment, PaymentRequest


@pytest.mark.asyncio
async def test_reservation_is_idempotent(repositories) -> None:
    _, _, sessions = repositories
    async with sessions.begin() as session:
        apartment = Apartment(
            lalafo_id=9001, source_url="https://source", phone="+996700000001",
            fingerprint="x", price=20000, rooms="1", district="ЦУМ",
            city="Бишкек", photo_urls=["https://img"],
        )
        session.add(apartment)
        await session.flush()
        apartment_id = apartment.id
    repo = FeaturedRepository(sessions)
    first = await repo.reserve(date(2026, 8, 27), 1, apartment_id, 9001)
    second = await repo.reserve(date(2026, 8, 27), 1, apartment_id, 9001)
    assert first.id == second.id
    assert await repo.daily_committed_budget(date(2026, 8, 27)) == 0
    assert await repo.reserve_campaign_budget(first.id, amount=200, daily_limit=400)
    assert not await repo.reserve_campaign_budget(first.id, amount=200, daily_limit=400)
    assert await repo.daily_committed_budget(date(2026, 8, 27)) == 200
    async with sessions() as session:
        assert not list(await session.scalars(__import__("sqlalchemy").select(PaymentRequest)))


@pytest.mark.asyncio
async def test_admin_selection_is_limited_to_two(repositories) -> None:
    _, _, sessions = repositories
    repo = FeaturedRepository(sessions)
    candidate_ids = []
    apartments = []
    async with sessions.begin() as session:
        for number in range(3):
            apartment = Apartment(
                lalafo_id=9100 + number, source_url=f"https://source/{number}",
                phone=f"+99670000000{number}", fingerprint=f"f{number}",
                price=20000, rooms="1", district="ЦУМ", city="Бишкек",
                photo_urls=["https://img"],
            )
            session.add(apartment)
            await session.flush()
            apartments.append((apartment.id, apartment.lalafo_id, apartment.source_url))
    for number, (apartment_id, lalafo_id, source_url) in enumerate(apartments):
        row = await repo.add_candidate(
            date(2026, 8, 27), apartment_id=apartment_id,
            lalafo_id=lalafo_id, source_url=source_url,
            source_payload={"lalafo_id": lalafo_id},
            rank=number + 1,
        )
        candidate_ids.append(row.id)
    assert (await repo.select_candidate(candidate_ids[0]))[0] == "selected"
    assert (await repo.select_candidate(candidate_ids[1]))[0] == "selected"
    assert (await repo.select_candidate(candidate_ids[2]))[0] == "full"
    selected = await repo.selected_candidates(date(2026, 8, 27))
    assert [row.selected_slot for row in selected] == [1, 2]


@pytest.mark.asyncio
async def test_review_cursor_advances_monotonically(repositories) -> None:
    _, _, sessions = repositories
    repo = FeaturedRepository(sessions)
    assert await repo.review_cursor() == 0
    await repo.advance_review_cursor(101)
    await repo.advance_review_cursor(99)
    assert await repo.review_cursor() == 101

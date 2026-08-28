from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.featured.repository import FeaturedRepository
from app.models import Apartment, DailyFeaturedPublication, PaymentRequest


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
async def test_reservation_keeps_different_source_when_slot_is_occupied(repositories) -> None:
    _, _, sessions = repositories
    apartment_ids: list[int] = []
    async with sessions.begin() as session:
        for number in range(2):
            apartment = Apartment(
                lalafo_id=9050 + number,
                source_url=f"https://source/{number}",
                phone=f"+99670000001{number}", fingerprint=f"slot-{number}",
                price=20000, rooms="1", district="ЦУМ", city="Бишкек",
                photo_urls=["https://img"],
            )
            session.add(apartment)
            await session.flush()
            apartment_ids.append(apartment.id)
    repo = FeaturedRepository(sessions)
    first = await repo.reserve(date(2026, 8, 27), 1, apartment_ids[0], 9050)
    second = await repo.reserve(date(2026, 8, 27), 1, apartment_ids[1], 9051)
    duplicate = await repo.reserve(date(2026, 8, 27), 2, apartment_ids[1], 9051)
    assert (first.slot, second.slot) == (1, 2)
    assert duplicate.id == second.id


@pytest.mark.asyncio
async def test_featured_lifecycle_notifications_are_one_time(repositories) -> None:
    _, _, sessions = repositories
    now = datetime.now(timezone.utc)
    async with sessions.begin() as session:
        apartment = Apartment(
            lalafo_id=9070, source_url="https://source/events",
            phone="+996700000070", fingerprint="events", price=25000,
            rooms="1", district="ЦУМ", city="Бишкек",
            photo_urls=["https://img"],
        )
        session.add(apartment)
        await session.flush()
        publication = DailyFeaturedPublication(
            business_date=date(2026, 8, 27), slot=1,
            source_apartment_id=apartment.id, source_lalafo_id=9070,
            managed_lalafo_ad_id=115700070,
            managed_lalafo_ad_url="https://lalafo.kg/bishkek/ads/id-115700070",
            lalafo_publication_status="active", telegram_message_id=70070,
            created_at=now - timedelta(hours=19),
        )
        session.add(publication)
        await session.flush()
        publication_id = publication.id

    repo = FeaturedRepository(sessions)
    assert [row.id for row in await repo.pending_new_notifications()] == [publication_id]
    assert [row.id for row in await repo.expiring_soon(now)] == [publication_id]

    await repo.patch(
        publication_id, new_ad_notified_at=now, expiring_notified_at=now
    )
    assert await repo.pending_new_notifications() == []
    assert await repo.expiring_soon(now) == []

    await repo.patch(publication_id, deactivated_at=now)
    assert [
        row.id for row in await repo.pending_deactivation_notifications()
    ] == [publication_id]
    await repo.patch(publication_id, deactivated_notified_at=now)
    assert await repo.pending_deactivation_notifications() == []


@pytest.mark.asyncio
async def test_queued_custom_link_is_auto_approved(repositories) -> None:
    _, _, sessions = repositories
    repo = FeaturedRepository(sessions)
    candidate = await repo.add_candidate(
        date(2026, 8, 27), apartment_id=None, lalafo_id=9060,
        source_url="https://source/custom",
        source_payload={"lalafo_id": 9060}, rank=0,
    )
    assert (await repo.select_custom_candidate(candidate.id))[0] == "selected"
    assert await repo.approve_custom_candidates(date(2026, 8, 27)) == 1
    assert await repo.approve_custom_candidates(date(2026, 8, 27)) == 0
    selected = await repo.selected_candidates(date(2026, 8, 27))
    assert [row.id for row in selected] == [candidate.id]


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
    assert (await repo.approve_candidate(candidate_ids[0]))[0] == "approved"
    assert (await repo.approve_candidate(candidate_ids[1]))[0] == "approved"
    selected = await repo.selected_candidates(date(2026, 8, 27))
    assert [row.selected_slot for row in selected] == [1, 2]


@pytest.mark.asyncio
async def test_custom_link_replaces_second_automatic_choice(repositories) -> None:
    _, _, sessions = repositories
    repo = FeaturedRepository(sessions)
    candidate_ids = []
    for number in range(3):
        row = await repo.add_candidate(
            date(2026, 8, 27), apartment_id=None, lalafo_id=9200 + number,
            source_url=f"https://source/{number}",
            source_payload={"lalafo_id": 9200 + number}, rank=number + 1,
        )
        candidate_ids.append(row.id)
    assert (await repo.select_candidate(candidate_ids[0]))[0] == "selected"
    assert (await repo.select_candidate(candidate_ids[1]))[0] == "selected"
    outcome, custom = await repo.select_custom_candidate(candidate_ids[2])
    assert outcome == "replaced"
    assert custom is not None and custom.selected_slot == 2
    assert (await repo.approve_candidate(candidate_ids[2]))[0] == "approved"
    selected = await repo.selected_candidates(date(2026, 8, 27))
    assert [row.id for row in selected] == [candidate_ids[2]]


@pytest.mark.asyncio
async def test_review_cursor_advances_monotonically(repositories) -> None:
    _, _, sessions = repositories
    repo = FeaturedRepository(sessions)
    assert await repo.review_cursor() == 0
    await repo.advance_review_cursor(101)
    await repo.advance_review_cursor(99)
    assert await repo.review_cursor() == 101

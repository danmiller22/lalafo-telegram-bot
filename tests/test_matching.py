from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.matching.repository import MatchingRepository, apartment_matches
from app.matching.handlers import show_hot_start
from app.config import Settings
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_hot_feed_returns_two_fresh_different_districts(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    first = await apartments.upsert_discovered(
        make_ad(lalafo_id=7001, district="Центр", price=30_000)
    )
    second = await apartments.upsert_discovered(
        make_ad(lalafo_id=7002, district="Асанбай", price=35_000)
    )
    third = await apartments.upsert_discovered(
        make_ad(lalafo_id=7003, district="Центр", price=32_000)
    )
    for item in (first, second, third):
        await apartments.mark_published(item.id, chat_id=-100, message_id=item.id)

    hot = await matching.hot_apartments()

    assert len(hot) == 2
    assert {item.district for item in hot} == {"Центр", "Асанбай"}


@pytest.mark.asyncio
async def test_new_customer_sees_two_hot_cards_before_filter_setup(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    for index, district in enumerate(("Центр", "Асанбай"), start=1):
        apartment = await apartments.upsert_discovered(
            make_ad(lalafo_id=7050 + index, district=district)
        )
        await apartments.mark_published(
            apartment.id, chat_id=-100, message_id=apartment.id
        )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=505),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(
            side_effect=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        )
    )
    state = SimpleNamespace(clear=AsyncMock(), update_data=AsyncMock())

    await show_hot_start(
        message,
        bot=bot,
        matching=matching,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(_env_file=None),
        state=state,
        source="lalafo",
    )

    assert message.answer.await_args_list[0].args[0] == "🔥 Свежие квартиры прямо сейчас"
    assert bot.send_photo.await_count == 2
    footer = message.answer.await_args_list[-1].kwargs["reply_markup"]
    assert footer.inline_keyboard[0][0].callback_data == "matching:start"


@pytest.mark.asyncio
async def test_profile_matches_rooms_district_and_max_budget(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    profile = await matching.save_profile(
        user_id=77,
        rooms=["1"],
        districts=["7 мкр"],
        all_districts=False,
        max_budget=30_000,
        source="lalafo",
    )
    matching_ad = await apartments.upsert_discovered(
        make_ad(lalafo_id=7101, district="7 микрорайон", price=29_000)
    )
    expensive = await apartments.upsert_discovered(
        make_ad(lalafo_id=7102, district="7 мкр", price=31_000)
    )
    studio = await apartments.upsert_discovered(
        make_ad(lalafo_id=7103, district="7 мкр", price=28_000, rooms="studio")
    )

    assert apartment_matches(matching_ad, profile)
    assert not apartment_matches(expensive, profile)
    assert not apartment_matches(studio, profile)


@pytest.mark.asyncio
async def test_manual_feed_does_not_repeat_recorded_apartment(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    profile = await matching.save_profile(
        user_id=88,
        rooms=["studio", "1"],
        districts=[],
        all_districts=True,
        max_budget=40_000,
        source="telegram",
    )
    first = await apartments.upsert_discovered(make_ad(lalafo_id=7201))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=7202))
    for item in (first, second):
        await apartments.mark_published(item.id, chat_id=-100, message_id=item.id)

    initial = await matching.matching_apartments(profile, limit=1)
    await matching.record_shown(88, initial, kind="initial")
    following = await matching.matching_apartments(profile, limit=3)

    assert [item.id for item in initial] == [second.id]
    assert [item.id for item in following] == [first.id]


@pytest.mark.asyncio
async def test_notification_outbox_only_queues_apartments_published_after_filter(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    old = await apartments.upsert_discovered(make_ad(lalafo_id=7301))
    await apartments.mark_published(old.id, chat_id=-100, message_id=1)
    await matching.save_profile(
        user_id=99,
        rooms=["1"],
        districts=[],
        all_districts=True,
        max_budget=40_000,
        source="telegram",
    )
    new = await apartments.upsert_discovered(make_ad(lalafo_id=7302))
    await apartments.mark_published(new.id, chat_id=-100, message_id=2)
    async with sessions.begin() as session:
        from app.models import Apartment, ApartmentSearchProfile

        now = datetime.now(timezone.utc)
        stored = await session.get(ApartmentSearchProfile, 99)
        stored.notifications_after = now - timedelta(seconds=5)
        stored_old = await session.get(Apartment, old.id)
        stored_old.published_at = now - timedelta(seconds=10)
        stored_new = await session.get(Apartment, new.id)
        stored_new.published_at = now

    assert await matching.seed_notifications() == 1
    claimed = await matching.claim_delivery()
    assert claimed is not None
    assert claimed.user_id == 99
    assert claimed.apartment.id == new.id

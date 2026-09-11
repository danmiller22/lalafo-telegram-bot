from datetime import datetime, timezone

import pytest

from scripts.publish_two_bedrooms import (
    TWO_BEDROOM_DAILY_LIMIT,
    TWO_BEDROOM_MIN_PRICE,
    TWO_BEDROOM_PRIMARY_MIN_PHOTOS,
    TWO_BEDROOM_SEARCH_URL,
    TWO_BEDROOM_SLOT_LIMIT,
    bishkek_day_window,
    bishkek_slot_target,
    published_two_bedrooms_today,
    select_two_bedroom_batch,
)
from tests.helpers import make_ad


def test_two_bedroom_source_and_limits_are_fixed() -> None:
    assert "/2-bedrooms/owner" in TWO_BEDROOM_SEARCH_URL
    assert "price[from]=20000" in TWO_BEDROOM_SEARCH_URL
    assert "price[to]=40000" in TWO_BEDROOM_SEARCH_URL
    assert TWO_BEDROOM_MIN_PRICE == 20_000
    assert TWO_BEDROOM_DAILY_LIMIT == 20
    assert TWO_BEDROOM_SLOT_LIMIT == 5
    assert TWO_BEDROOM_PRIMARY_MIN_PHOTOS == 4


def test_two_bedroom_batch_uses_strong_photo_cards_before_fallback() -> None:
    strong = [
        make_ad(lalafo_id=index, rooms="2", photo_urls=[str(index)] * 4)
        for index in range(1, 5)
    ]
    fallback = make_ad(lalafo_id=5, rooms="2", photo_urls=["one"])
    rejected = [
        make_ad(lalafo_id=6, rooms="1", photo_urls=["x"] * 4),
        make_ad(lalafo_id=7, rooms="2", price=40_001, photo_urls=["x"] * 4),
        make_ad(lalafo_id=8, rooms="2", owner_listing=False, photo_urls=["x"] * 4),
        make_ad(lalafo_id=9, rooms="2", price=19_999, photo_urls=["x"] * 4),
    ]

    selected = select_two_bedroom_batch(strong + [fallback] + rejected, 5)

    assert {ad.lalafo_id for ad in selected} == {1, 2, 3, 4, 5}
    assert selected[-1].lalafo_id == 5


def test_bishkek_calendar_day_is_converted_to_utc() -> None:
    start, end = bishkek_day_window(datetime(2026, 9, 11, 12, tzinfo=timezone.utc))

    assert start == datetime(2026, 9, 10, 18, tzinfo=timezone.utc)
    assert end.date().isoformat() == "2026-09-11"
    assert end.hour == 17


@pytest.mark.parametrize(
    ("utc_hour", "expected"),
    [
        (2, None),
        (3, ("2026-09-11:09", 5)),
        (7, ("2026-09-11:13", 10)),
        (11, ("2026-09-11:17", 15)),
        (15, ("2026-09-11:21", 20)),
    ],
)
def test_bishkek_slots_have_cumulative_daily_targets(utc_hour, expected) -> None:
    assert bishkek_slot_target(
        datetime(2026, 9, 11, utc_hour, tzinfo=timezone.utc)
    ) == expected


@pytest.mark.asyncio
async def test_daily_count_includes_only_two_bedrooms_from_bishkek_day(
    repositories,
) -> None:
    apartments, _, sessions = repositories
    inside = await apartments.upsert_discovered(make_ad(lalafo_id=101, rooms="2"))
    one_room = await apartments.upsert_discovered(make_ad(lalafo_id=102, rooms="1"))
    await apartments.mark_published(inside.id, chat_id=-1001, message_id=1)
    await apartments.mark_published(one_room.id, chat_id=-1001, message_id=2)

    count = await published_two_bedrooms_today(
        sessions,
        now=datetime.now(timezone.utc),
    )

    assert count == 1

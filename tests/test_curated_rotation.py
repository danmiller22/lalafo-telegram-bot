from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_curated_rotation_resolves_latest_original_phone_backed_cards(
    repositories,
) -> None:
    apartments, _, _ = repositories
    older = make_ad(
        lalafo_id=101,
        district="Филармония",
        price=25_000,
        rooms="1",
        phone="+996500000101",
        photo_urls=["https://img/old"],
        source_updated_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    newer = make_ad(
        lalafo_id=102,
        district="Филармония, центр",
        price=25_000,
        rooms="1",
        phone="+996500000102",
        photo_urls=["https://img/new"],
        source_updated_at=datetime.now(timezone.utc),
    )
    mossovet = make_ad(
        lalafo_id=103,
        district="Моссовет",
        price=20_000,
        rooms="1",
        phone="+996500000103",
        photo_urls=["https://img/mossovet"],
    )
    ignored_phone_hidden_repost = make_ad(
        lalafo_id=104,
        district="Моссовет",
        price=20_000,
        rooms="1",
        phone="",
        photo_urls=["https://img/repost"],
    )
    for ad in (older, newer, mossovet, ignored_phone_hidden_repost):
        await apartments.upsert_discovered(ad)

    selected = await apartments.curated_rotation_apartments(
        (("Филармония", 25_000), ("Моссовет", 20_000))
    )

    assert [item.lalafo_id for item in selected] == [102, 103]
    assert all(item.phone for item in selected)


@pytest.mark.asyncio
async def test_curated_rotation_resolves_explicitly_approved_ids(repositories) -> None:
    apartments, _, _ = repositories
    first = await apartments.upsert_discovered(
        make_ad(lalafo_id=115333471, rooms="1", price=30_000)
    )
    second = await apartments.upsert_discovered(
        make_ad(lalafo_id=114091573, rooms="1", price=26_000)
    )

    selected = await apartments.curated_rotation_apartments_by_ids(
        (114091573, 115333471, 999999)
    )

    assert [item.id for item in selected] == [second.id, first.id]

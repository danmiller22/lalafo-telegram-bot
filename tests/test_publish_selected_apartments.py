from __future__ import annotations

import pytest

from tools.publish_selected_apartments import (
    SELECTED_REPOST_AFTER_HOURS,
    _force_repost,
    _search_request_announcement_requested,
    eligible_selected_listings,
    selected_managed_ad_ids,
    selected_listings,
)


def test_selected_listings_accepts_deduplicated_lalafo_urls() -> None:
    url_a = "https://lalafo.kg/bishkek/ads/first-id-115863328?feed_id=5012"
    url_b = "https://www.lalafo.kg/bishkek/ads/second-id-115838403"

    result = selected_listings(f"{url_a}\n{url_b}\n{url_a}")

    assert [(item.lalafo_id, item.url) for item in result] == [
        (115863328, url_a),
        (115838403, url_b),
    ]


def test_selected_publication_reposts_only_explicitly_allowed_cards() -> None:
    selected = selected_listings(
        "https://lalafo.kg/bishkek/ads/first-id-115863328 "
        "https://lalafo.kg/bishkek/ads/second-id-115838403 "
        "https://lalafo.kg/bishkek/ads/third-id-115838404"
    )

    eligible, recent = eligible_selected_listings(
        selected,
        published_ids={115863328, 115838403},
        repostable_ids={115838403},
    )

    assert SELECTED_REPOST_AFTER_HOURS is None
    assert [item.lalafo_id for item in eligible] == [115838403, 115838404]
    assert [item.lalafo_id for item in recent] == [115863328]


def test_force_repost_can_make_every_selected_card_eligible() -> None:
    selected = selected_listings(
        "https://lalafo.kg/bishkek/ads/first-id-115863328 "
        "https://lalafo.kg/bishkek/ads/second-id-115838403"
    )

    eligible, recent = eligible_selected_listings(
        selected,
        published_ids=set(),
        repostable_ids=set(),
    )

    assert [item.lalafo_id for item in eligible] == [115863328, 115838403]
    assert recent == []


def test_force_repost_marker_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_SELECTED_REPOST", raising=False)

    assert not _force_repost("https://lalafo.kg/ads/example-id-1")
    assert _force_repost(
        "https://lalafo.kg/ads/example-id-1?codex_force_repost=1"
    )


def test_search_request_announcement_marker_is_explicit() -> None:
    assert not _search_request_announcement_requested(
        "https://lalafo.kg/ads/example-id-1"
    )
    assert _search_request_announcement_requested(
        "https://lalafo.kg/ads/example-id-1?codex_announcement=search_request"
    )


def test_selected_managed_ad_ids_are_explicit_and_deduplicated() -> None:
    assert selected_managed_ad_ids("116308426, 116308347 116308426") == {
        116308426,
        116308347,
    }

    with pytest.raises(ValueError, match="Unsupported managed"):
        selected_managed_ad_ids("all")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://lalafo.kg/bishkek/ads/example-id-1",
        "https://example.com/bishkek/ads/example-id-1",
        "https://lalafo.kg/bishkek/ads/missing-id",
    ],
)
def test_selected_listings_rejects_unsafe_or_incomplete_urls(value: str) -> None:
    with pytest.raises(ValueError):
        selected_listings(value)


def test_selected_listings_rejects_permanently_excluded_source() -> None:
    with pytest.raises(ValueError, match="permanently excluded"):
        selected_listings(
            "https://lalafo.kg/bishkek/ads/example-id-115809037"
        )


@pytest.mark.parametrize("lalafo_id", [115884595, 112298605])
def test_selected_listings_rejects_withdrawn_filarmoniya_card(lalafo_id: int) -> None:
    with pytest.raises(ValueError, match="permanently excluded"):
        selected_listings(
            f"https://lalafo.kg/bishkek/ads/filarmoniya-id-{lalafo_id}"
        )

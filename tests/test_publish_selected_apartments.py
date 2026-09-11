from __future__ import annotations

import pytest

from tools.publish_selected_apartments import (
    SELECTED_REPOST_AFTER_HOURS,
    eligible_selected_listings,
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


def test_selected_publication_obeys_the_six_hour_repost_cooldown() -> None:
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

    assert SELECTED_REPOST_AFTER_HOURS == 6.0
    assert [item.lalafo_id for item in eligible] == [115838403, 115838404]
    assert [item.lalafo_id for item in recent] == [115863328]


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

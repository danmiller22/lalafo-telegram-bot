from __future__ import annotations

import pytest

from tools.publish_selected_apartments import selected_listings


def test_selected_listings_accepts_deduplicated_lalafo_urls() -> None:
    url_a = "https://lalafo.kg/bishkek/ads/first-id-115863328?feed_id=5012"
    url_b = "https://www.lalafo.kg/bishkek/ads/second-id-115838403"

    result = selected_listings(f"{url_a}\n{url_b}\n{url_a}")

    assert [(item.lalafo_id, item.url) for item in result] == [
        (115863328, url_a),
        (115838403, url_b),
    ]


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

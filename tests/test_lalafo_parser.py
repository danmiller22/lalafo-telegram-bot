import json

from app.lalafo.parser import (
    parse_detail_data,
    parse_detail_page,
    parse_search_data,
    parse_search_page,
)


def next_html(query_key, data):
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"queryKey": query_key, "state": {"data": data}}]
                }
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


def test_parse_search_page():
    html = next_html(
        ["listingFeed", "query"],
        {
            "pages": [
                {
                    "items": [
                        {
                            "id": 42,
                            "url": "/bishkek/ads/test-id-42",
                            "price": 30000,
                            "currency": "KGS",
                            "city": "Бишкек",
                            "images": [{"original_url": "https://img.example/1.jpg"}],
                            "updated_time": 1_700_000_000,
                        }
                    ],
                    "_meta": {"totalCount": 181, "currentPage": 1, "pageCount": 8},
                }
            ]
        },
    )
    page = parse_search_page(html)
    assert page.total == 181
    assert page.page_count == 8
    assert page.items[0].detail_url == "https://lalafo.kg/bishkek/ads/test-id-42"


def test_parse_public_feed_json():
    page = parse_search_data(
        {
            "items": [
                {
                    "id": 43,
                    "url": "/bishkek/ads/test-id-43",
                    "price": 31000,
                    "currency": "KGS",
                    "city": "Бишкек",
                    "images": [{"original_url": "https://img.example/2.jpg"}],
                }
            ],
            "_meta": {"totalCount": 191, "currentPage": 1, "pageCount": 10},
        }
    )
    assert page.total == 191
    assert page.items[0].lalafo_id == 43


def test_parse_detail_page_uses_structured_fields_and_description_deposit():
    html = next_html(
        ["detail", 12, "ru_RU", 42],
        {
            "id": 42,
            "category_id": 2044,
            "mobile": "0555123456",
            "price": 30000,
            "currency": "KGS",
            "city": "Бишкек",
            "description": "Аренда 30 000, залог: 15 000",
            "params": [
                {"name": "Количество комнат", "value": "1 комната"},
                {"name": "Район Бишкека", "value": "7 мкр"},
                {"name": "Для кого", "value": "Без подселения, Семейным"},
            ],
            "images": [{"original_url": "https://img.example/1.jpg"}],
            "created_time": 1_700_000_000,
            "updated_time": 1_700_000_100,
        },
    )
    ad = parse_detail_page(html, source_url="https://lalafo.kg/id-42")
    assert ad.rooms == "1"
    assert ad.district == "7 мкр"
    assert ad.deposit == 15000
    assert ad.phone == "+996555123456"
    assert ad.no_subletting


def test_parse_public_detail_json():
    ad = parse_detail_data(
        {
            "id": 44,
            "category_id": 2044,
            "mobile": "+996555123456",
            "price": 32000,
            "currency": "KGS",
            "city": "Бишкек",
            "params": [
                {"name": "Количество комнат", "value": "Студия"},
                {"name": "Для кого", "value": "Без подселения"},
                {"name": "Депозит, сом", "value": "10000"},
            ],
            "images": [{"original_url": "https://img.example/3.jpg"}],
        },
        source_url="https://lalafo.kg/bishkek/ads/test-id-44",
    )
    assert ad.rooms == "studio"
    assert ad.deposit == 10000

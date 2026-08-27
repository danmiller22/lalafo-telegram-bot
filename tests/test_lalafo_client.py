import json
from unittest.mock import AsyncMock

import pytest

from app.config import DEFAULT_SEARCH_URL
from app.lalafo.client import LalafoClient, LalafoError


SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/2-bedrooms/studio/owner/"
    "real-estate-agency/bez-podseleniya?price[to]=35000"
)


def test_search_params_preserve_configured_filters():
    params = dict(LalafoClient._search_params(SEARCH_URL, 3))
    assert params["category_id"] == "2044"
    assert params["city_id"] == "103184"
    assert params["page"] == "3"
    assert params["price[to]"] == "35000"
    assert set(params[key] for key in params if key.startswith("parameters[69]")) == {
        "15496",
        "2773",
        "2774",
    }
    assert set(params[key] for key in params if key.startswith("parameters[2149]")) == {
        "19057",
        "42340",
    }
    assert params["parameters[946][0]"] == "81537"


def test_default_search_uses_all_selected_districts():
    params = dict(LalafoClient._search_params(DEFAULT_SEARCH_URL, 1))
    district_values = [
        value for key, value in params.items() if key.startswith("parameters[357][")
    ]
    assert len(district_values) == 113
    assert district_values[0] == "30232"
    assert district_values[-1] == "56412"
    assert params["price[to]"] == "35000"


@pytest.mark.parametrize(
    ("offerer", "expected"),
    [("realtor", "42340"), ("owner", "19057")],
)
def test_search_params_can_target_each_offerer_independently(offerer, expected):
    params = dict(LalafoClient._search_params(SEARCH_URL, 1, offerer))
    offerer_values = [
        value for key, value in params.items() if key.startswith("parameters[2149]")
    ]
    assert offerer_values == [expected]


def detail_html(ad_id: int, phone: str) -> str:
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "queryKey": ["detail", 12, "ru_RU", ad_id],
                            "state": {
                                "data": {
                                    "id": ad_id,
                                    "category_id": 2044,
                                    "mobile": phone,
                                    "price": 30000,
                                    "currency": "KGS",
                                    "city": "Бишкек",
                                    "params": [
                                        {"name": "Количество комнат", "value": "1 комната"},
                                        {"name": "Для кого", "value": "Без подселения"},
                                        {"name": "Кто предлагает", "value": "Собственник"},
                                    ],
                                    "images": [{"original_url": "https://img.example/1.jpg"}],
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script></html>"
    )


@pytest.mark.asyncio
async def test_detail_uses_matching_page_phone():
    client = LalafoClient()
    client._get_text = AsyncMock(return_value=detail_html(77701377, "+996554252534"))
    try:
        ad = await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701377")
    finally:
        await client.close()

    assert ad.lalafo_id == 77701377
    assert ad.phone == "+996554252534"


@pytest.mark.asyncio
async def test_detail_rejects_mismatched_page():
    client = LalafoClient()
    client._get_text = AsyncMock(return_value=detail_html(43393050, "+996555000617"))
    try:
        with pytest.raises(LalafoError, match="detail mismatch"):
            await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701377")
    finally:
        await client.close()

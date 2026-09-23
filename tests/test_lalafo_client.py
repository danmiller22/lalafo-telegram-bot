from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import ADDITIONAL_SEARCH_URLS, DEFAULT_SEARCH_URL
from app.lalafo.client import LalafoAccessError, LalafoClient, LalafoError


SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/2-bedrooms/studio/owner/"
    "real-estate-agency/bez-podseleniya?price[to]=35000"
)

DISTRICT_SEARCH_URL = (
    "https://lalafo.kg/bishkek/kvartiry/arenda-kvartir/"
    "dolgosrochnaya-arenda-kvartir/1-bedroom/studio/filarmoniya/tsum"
    "?price[from]=18000&price[to]=35000"
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


def test_primary_search_uses_owner_studio_one_bedroom_and_price_filters():
    params = dict(LalafoClient._search_params(DEFAULT_SEARCH_URL, 1))
    assert set(
      params[key] for key in params if key.startswith("parameters[69]")
    ) == {"15496", "2773"}
    assert [
        params[key] for key in params if key.startswith("parameters[2149]")
    ] == ["19057"]
    assert params["price[from]"] == "20000"
    assert params["price[to]"] == "40000"


def test_client_sends_browser_context_headers():
    client = LalafoClient()
    assert client._headers["Origin"] == "https://lalafo.kg"
    assert client._headers["Referer"] == "https://lalafo.kg/"


def test_supplementary_search_reserves_owner_and_realtor_pools():
    assert len(ADDITIONAL_SEARCH_URLS) == 2
    for url, expected_offerer in zip(ADDITIONAL_SEARCH_URLS, ("19057", "42340")):
        params = dict(LalafoClient._search_params(url, 1))
        assert set(
            params[key] for key in params if key.startswith("parameters[69]")
        ) == {"15496", "2773"}
        assert [
            params[key] for key in params if key.startswith("parameters[2149]")
        ] == [expected_offerer]
        assert not any(key.startswith("parameters[946]") for key in params)
        assert params["price[from]"] == "20000"
        assert params["price[to]"] == "40000"


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


@pytest.mark.asyncio
async def test_search_retries_without_long_district_filter_after_access_error():
    client = LalafoClient()
    client._get_json = AsyncMock(
        side_effect=[
            LalafoAccessError("blocked long query"),
            {"items": [], "_meta": {"total_count": 0, "page_count": 1}},
        ]
    )
    try:
        await client.search(DISTRICT_SEARCH_URL, page=1)
    finally:
        await client.close()

    first_url, second_url = [call.args[0] for call in client._get_json.await_args_list]
    assert "parameters%5B357%5D" in first_url
    assert "parameters%5B357%5D" not in second_url
    assert "parameters%5B69%5D" in second_url
    assert "price%5Bto%5D=35000" in second_url


def detail_payload(ad_id: int, phone: str) -> dict:
    return {
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


@pytest.mark.asyncio
async def test_detail_uses_matching_page_phone():
    client = LalafoClient()
    client._get_text = AsyncMock(return_value="<html></html>")
    try:
        with patch(
            "app.lalafo.client.parse_detail_page",
            return_value=SimpleNamespace(
                lalafo_id=77701377, phone="+996554252534"
            ),
        ):
            ad = await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701377")
    finally:
        await client.close()

    assert ad.lalafo_id == 77701377
    assert ad.phone == "+996554252534"


@pytest.mark.asyncio
async def test_detail_rejects_mismatched_page():
    client = LalafoClient()
    client._get_text = AsyncMock(return_value="<html></html>")
    try:
        with patch(
            "app.lalafo.client.parse_detail_page",
            return_value=SimpleNamespace(
                lalafo_id=43393050, phone="+996555000617"
            ),
        ):
            with pytest.raises(LalafoError, match="detail mismatch"):
                await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701377")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detail_falls_back_to_api_when_browser_is_blocked():
    client = LalafoClient()
    client._get_text = AsyncMock(side_effect=LalafoAccessError("HTTP 403"))
    client._get_json = AsyncMock(
        return_value=detail_payload(77701377, "+996554252534")
    )
    try:
        ad = await client.detail(
            "https://lalafo.kg/bishkek/ads/example-id-77701377?feed_id=5012"
        )
    finally:
        await client.close()

    assert ad.lalafo_id == 77701377
    assert ad.phone == "+996554252534"
    assert "/api/search/v3/feed/details/77701377" in client._get_json.await_args.args[0]


@pytest.mark.asyncio
async def test_detail_api_fallback_rejects_mismatched_id():
    client = LalafoClient()
    client._get_text = AsyncMock(side_effect=LalafoAccessError("HTTP 403"))
    client._get_json = AsyncMock(
        return_value=detail_payload(43393050, "+996555000617")
    )
    try:
        with pytest.raises(LalafoError, match="detail mismatch"):
            await client.detail(
                "https://lalafo.kg/bishkek/ads/example-id-77701377"
            )
    finally:
        await client.close()

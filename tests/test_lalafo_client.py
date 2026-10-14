from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import httpx

from app.config import ADDITIONAL_SEARCH_URLS, DEFAULT_SEARCH_URL, INVENTORY_SEARCH_URLS
from app.lalafo.client import LalafoAccessError, LalafoClient, LalafoError
from socksio.exceptions import ProtocolError as SocksProtocolError


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
    assert params["price[from]"] == "25000"
    assert params["price[to]"] == "40000"


def test_client_sends_browser_context_headers():
    client = LalafoClient()
    assert client._headers["Origin"] == "https://lalafo.kg"
    assert client._headers["Referer"] == "https://lalafo.kg/"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_get_json", "_get_text"])
async def test_access_error_retains_http_status_for_collector_report(method):
    client = LalafoClient(max_retries=0)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, request=request),
    ))
    client._get_json_via_relay = AsyncMock(return_value=None)
    try:
        with pytest.raises(LalafoAccessError) as error:
            await getattr(client, method)("https://lalafo.kg/api/search/v3/feed/search")
        assert error.value.status_code == 403
    finally:
        await client.close()


def test_supplementary_search_uses_broad_owner_pool():
    assert len(ADDITIONAL_SEARCH_URLS) == 1
    for url in ADDITIONAL_SEARCH_URLS:
        params = dict(LalafoClient._search_params(url, 1))
        assert set(
            params[key] for key in params if key.startswith("parameters[69]")
        ) == {"15496", "2773"}
        assert [params[key] for key in params if key.startswith("parameters[2149]")] == ["42340"]
        assert not any(key.startswith("parameters[946]") for key in params)
        assert params["price[from]"] == "25000"
        assert params["price[to]"] == "40000"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_get_json", "_get_text"])
async def test_socks_protocol_failure_is_reported_as_retryable_lalafo_error(method):
    client = LalafoClient(max_retries=0)
    client._client.get = AsyncMock(side_effect=SocksProtocolError("Malformed reply"))
    try:
        with pytest.raises(LalafoError, match="ProtocolError"):
            await getattr(client, method)("https://lalafo.kg/api/search/v3/feed/search")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_source_does_not_discard_proxy_pool_for_next_source(monkeypatch):
    client = LalafoClient(max_retries=1, proxy_url="http://route-1,http://route-2")
    await client._client.aclose()
    routed_requests = []

    def make_client():
        route = client._proxy_urls[client._proxy_index] if client._proxy_urls else None
        def respond(request):
            routed_requests.append((request.url.path, route))
            if request.url.path == "/failed" or route is None:
                return httpx.Response(403, request=request)
            return httpx.Response(200, json={"items": []}, request=request)
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    client._make_client = make_client
    client._client = make_client()
    client._get_json_via_relay = AsyncMock(return_value=None)
    monkeypatch.setattr("app.lalafo.client.asyncio.sleep", AsyncMock())
    try:
        with pytest.raises(LalafoAccessError):
            await client._get_json("https://lalafo.kg/failed")
        assert await client._get_json("https://lalafo.kg/next") == {"items": []}
        assert routed_requests[-1] == ("/next", "http://route-1")
    finally:
        await client.close()


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
async def test_blocked_browser_is_not_retried_for_every_card():
    client = LalafoClient()
    client._get_text = AsyncMock(side_effect=LalafoAccessError("HTTP 403"))
    client._get_json = AsyncMock(side_effect=[
        detail_payload(77701377, "+996554252534"),
        detail_payload(77701378, "+996555111222"),
    ])
    try:
        first = await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701377")
        second = await client.detail("https://lalafo.kg/bishkek/ads/example-id-77701378")
    finally:
        await client.close()
    client._get_text.assert_awaited_once()
    assert first.lalafo_id == 77701377
    assert second.lalafo_id == 77701378
    assert second.phone == "+996555111222"


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



def test_inventory_search_is_limited_to_studios_and_one_room():
    for url in INVENTORY_SEARCH_URLS:
        params = LalafoClient._search_params(url, 1)
        assert {value for key,value in params if key.startswith("parameters[69]")} == {"15496", "2773"}

@pytest.mark.asyncio
async def test_detail_html_parser_does_not_block_customer_event_loop(monkeypatch):
    import asyncio
    import threading
    from tests.helpers import make_ad
    from app.lalafo import client as source
    started, release = threading.Event(), threading.Event()
    def slow_parse(*args, **kwargs):
        started.set()
        release.wait(timeout=1)
        return make_ad()
    monkeypatch.setattr(source,'parse_detail_page',slow_parse)
    async with LalafoClient() as client:
        monkeypatch.setattr(client,'_get_text',AsyncMock(return_value='<html></html>'))
        task=asyncio.create_task(client.detail('https://lalafo.kg/bishkek/ads/example-id-12345'))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, timeout=0.5), timeout=0.7)
            assert started.is_set()
            assert not task.done()
        finally:
            release.set()
            result=await task
        assert result.lalafo_id==12345

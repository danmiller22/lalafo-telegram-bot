from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts import select_lalafo_proxy as selector


class HTTPClientStub:
    def __init__(self, responses):
        self.get = AsyncMock(side_effect=responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_selector_keeps_multiple_verified_routes(monkeypatch):
    client = HTTPClientStub([SimpleNamespace(text="\n".join(f"http://8.8.8.{n}:8080" for n in range(1, 5)), raise_for_status=lambda: None)])
    monkeypatch.setattr(selector.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(selector, "_works", AsyncMock(side_effect=lambda proxy: proxy))
    routes = await selector.find_working_proxies()
    assert len(routes) == 3
    assert len(set(routes)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("received_id,expected", [(123, "http://route"), (999, None)])
async def test_proxy_probe_requires_matching_detail_identity(monkeypatch, received_id, expected):
    client = HTTPClientStub([
        httpx.Response(200, json={"items": [{"url": "/bishkek/ads/example-id-123"}]}),
        httpx.Response(200, json={"id": received_id}),
    ])
    monkeypatch.setattr(selector.httpx, "AsyncClient", lambda **kwargs: client)
    assert await selector._works("http://route") == expected


@pytest.mark.asyncio
async def test_proxy_scan_bounds_parallel_connections(monkeypatch):
    client = HTTPClientStub([SimpleNamespace(text="\n".join(f"http://8.8.4.{n+1}:8080" for n in range(80)), raise_for_status=lambda: None)])
    monkeypatch.setattr(selector.httpx, "AsyncClient", lambda **kwargs: client)
    active = peak = 0

    async def unavailable(proxy):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return None
        finally:
            active -= 1

    monkeypatch.setattr(selector, "_works", unavailable)
    assert await selector.find_working_proxies() == []
    assert 1 < peak <= selector.MAX_CONCURRENT_PROBES
    assert active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
async def test_configured_proxy_is_verified_without_replacement_by_free_pool(monkeypatch, healthy):
    probe = AsyncMock(return_value="http://configured" if healthy else None)
    provider = AsyncMock(side_effect=AssertionError("configured routes must not use the free provider"))
    monkeypatch.setattr(selector, "_works", probe)
    monkeypatch.setattr(selector.httpx, "AsyncClient", provider)
    routes = await selector.find_working_proxies("http://configured,http://configured")
    assert routes == (["http://configured"] if healthy else [])
    probe.assert_awaited_once_with("http://configured", timeout=10.0)
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_provider_filters_private_addresses_and_accepts_socks5():
    response = httpx.Response(200, json={"data": [
        {"ip": "8.8.8.8", "port": "1080", "protocols": ["socks5"]},
        {"ip": "127.0.0.1", "port": "1080", "protocols": ["socks5"]},
        {"ip": "192.168.1.1", "port": "8080", "protocols": ["http"]},
    ]}, request=httpx.Request("GET", selector.GEONODE_URL))
    client = HTTPClientStub([response])
    assert await selector._proxy_candidates(client, selector.GEONODE_URL) == ["socks5://8.8.8.8:1080"]


@pytest.mark.asyncio
async def test_proxy_probe_rejects_empty_inventory(monkeypatch):
    client = HTTPClientStub([httpx.Response(200, json={"items": []})])
    monkeypatch.setattr(selector.httpx, "AsyncClient", lambda **kwargs: client)
    assert await selector._works("http://route") is None

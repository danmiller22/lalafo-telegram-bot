from __future__ import annotations

import asyncio
import ipaddress
from itertools import zip_longest
import json
import os
import re
import ssl
import sys
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from socksio.exceptions import ProtocolError as SocksProtocolError

from app.config import INVENTORY_SEARCH_URLS
from app.lalafo.client import LalafoClient


PROXY_LIST_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get"
GEONODE_URL = "https://proxylist.geonode.com/api/proxy-list"
PROXIFLY_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"
SEARCH_URL = "https://lalafo.kg/api/search/v3/feed/search?" + urlencode(
    LalafoClient._search_params(INVENTORY_SEARCH_URLS[0], 1)
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "device": "pc",
    "language": "ru_RU",
    "country-id": "12",
    "content-type": "application/json",
    "X-Cache-Bypass": "yes",
    "Origin": "https://lalafo.kg",
    "Referer": "https://lalafo.kg/",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-requested-with": "XMLHttpRequest",
}
# Keep several independently verified routes. Lalafo can accept the probe and
# then rate-limit that IP on the real multi-page search; LalafoClient rotates
# this comma-separated pool on 403/429 and transport failures.
TARGET_PROXY_COUNT = 3
MAX_CONCURRENT_PROBES = 24
# Reuse the trust store instead of loading hundreds of certificate bundles
# simultaneously on the small always-on bot instance.
TLS_CONTEXT = ssl.create_default_context()


def _public_proxy(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https", "socks5"}
                and parsed.port is not None and not parsed.username and not parsed.password
                and ipaddress.ip_address(parsed.hostname or "").is_global)
    except ValueError:
        return False


async def _proxy_candidates(client: httpx.AsyncClient, url: str, params: dict | None = None) -> list[str]:
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        if url == GEONODE_URL:
            candidates = [f"{protocol}://{row['ip']}:{row['port']}"
                          for row in response.json().get("data", [])
                          for protocol in row.get("protocols", [])]
        else:
            candidates = response.text.splitlines()[:200]
        return list(dict.fromkeys(value.strip() for value in candidates if _public_proxy(value.strip())))
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, StopAsyncIteration):
        return []


async def _works(proxy_url: str, *, timeout: float = 4.0) -> str | None:
    headers = dict(HEADERS)
    headers["user-hash"] = str(uuid.uuid4())
    headers["request-id"] = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            verify=TLS_CONTEXT,
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        ) as client:
            response = await client.get(SEARCH_URL)
            if response.status_code != 200:
                return None
            payload: Any = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                return None
            items = payload["items"]
            if not items or not isinstance(items[0], dict) or not items[0].get("url"):
                return None
            if items and isinstance(items[0], dict) and items[0].get("url"):
                detail_url = str(items[0]["url"])
                match = re.search(r"-id-(\d+)(?:$|[/?#])", detail_url)
                if not match:
                    return None
                detail_api_url = (
                    f"https://lalafo.kg/api/search/v3/feed/details/{match.group(1)}?expand=url"
                )
                detail = await client.get(
                    detail_api_url, headers={"request-id": str(uuid.uuid4())}
                )
                if detail.status_code != 200:
                    return None
                detail_payload = detail.json()
                if not isinstance(detail_payload, dict) or str(detail_payload.get("id")) != match.group(1):
                    return None
            return proxy_url
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, SocksProtocolError):
        return None


async def find_working_proxies(configured_proxy_url: str = "") -> list[str]:
    configured = list(dict.fromkeys(
        value.strip() for value in re.split(r"[,\n]+", configured_proxy_url) if value.strip()
    ))
    if configured:
        # An operator-configured route must never be replaced by a random free
        # proxy. Verify both search and matching detail through that route.
        results = await asyncio.gather(*(
            _works(proxy, timeout=10.0) for proxy in configured[:MAX_CONCURRENT_PROBES]
        ))
        return [proxy for proxy in results if proxy][:TARGET_PROXY_COUNT]
    params = {
        "request": "display_proxies",
        "protocol": "http",
        "proxy_format": "protocolipport",
        "format": "text",
        "ssl": "yes",
        "anonymity": "elite,anonymous",
        "timeout": "5000",
        "limit": "200",
    }
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        sources = await asyncio.gather(
            _proxy_candidates(client, PROXY_LIST_URL, params),
            _proxy_candidates(client, GEONODE_URL, {
                "limit": "50", "page": "1", "sort_by": "lastChecked",
                "sort_type": "desc", "protocols": "socks5",
            }),
            _proxy_candidates(client, PROXIFLY_URL),
        )
    # Interleave providers so a slow or dead first list cannot monopolise the
    # bounded scan before SOCKS5 routes get a chance to be verified.
    proxies = list(dict.fromkeys(
        proxy for group in zip_longest(*sources) for proxy in group if proxy
    ))[:200]
    if not proxies:
        return []

    selected: list[str] = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def probe(proxy: str) -> str | None:
        async with semaphore:
            return await _works(proxy)

    tasks = [asyncio.create_task(probe(proxy)) for proxy in proxies]
    try:
        # Allow multiple bounded batches and both search/detail probes.
        for task in asyncio.as_completed(tasks, timeout=30.0):
            result = await task
            if result:
                selected.append(result)
                if len(selected) >= TARGET_PROXY_COUNT:
                    return selected[:TARGET_PROXY_COUNT]
    except asyncio.TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return selected[:TARGET_PROXY_COUNT]


async def run() -> int:
    selected = await find_working_proxies(os.getenv("LALAFO_PROXY_URL", ""))
    if selected:
        print(f"LALAFO_PROXY_URL={','.join(selected)}")
        return 0
    print("No working Lalafo proxy was found", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

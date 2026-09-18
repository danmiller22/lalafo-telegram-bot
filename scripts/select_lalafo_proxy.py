from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from typing import Any

import httpx


PROXY_LIST_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get"
SEARCH_URL = (
    "https://lalafo.kg/api/search/v3/feed/search?expand=url&per-page=1&"
    "category_id=2044&page=1&city_id=103184&parameters%5B69%5D%5B0%5D=15496&"
    "parameters%5B69%5D%5B1%5D=2773&"
    "price%5Bto%5D=35000&with_feed_banner=true"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "device": "pc",
    "language": "ru_RU",
    "country-id": "12",
    "content-type": "application/json",
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
TARGET_PROXY_COUNT = 5


async def _works(proxy_url: str) -> str | None:
    headers = dict(HEADERS)
    headers["user-hash"] = str(uuid.uuid4())
    headers["request-id"] = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            headers=headers,
            timeout=httpx.Timeout(8.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(SEARCH_URL)
            if response.status_code != 200:
                return None
            payload: Any = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                return None
            items = payload["items"]
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
                if not isinstance(detail_payload, dict):
                    return None
            return proxy_url
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return None


async def find_working_proxies() -> list[str]:
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
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(PROXY_LIST_URL, params=params)
        response.raise_for_status()
    proxies = [line.strip() for line in response.text.splitlines() if line.strip()]
    if not proxies:
        return []

    selected: list[str] = []
    tasks = [asyncio.create_task(_works(proxy)) for proxy in proxies]
    try:
        for task in asyncio.as_completed(tasks, timeout=25.0):
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
    selected = await find_working_proxies()
    if selected:
        print(f"LALAFO_PROXY_URL={','.join(selected)}")
        return 0
    print("No working Lalafo proxy was found", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

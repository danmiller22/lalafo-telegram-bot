from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.lalafo.parser import parse_detail_page, parse_search_data

logger = logging.getLogger(__name__)


class LalafoError(RuntimeError):
    pass


class LalafoAccessError(LalafoError):
    pass


class LalafoNotFound(LalafoError):
    pass


class LalafoClient:
    def __init__(self, *, timeout: float = 25.0, max_retries: int = 3) -> None:
        self.max_retries = max_retries
        self._user_hash = str(uuid.uuid4())
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                # Required request context used by Lalafo's own web client.
                "device": "pc",
                "language": "ru_RU",
                "country-id": "12",
                "user-hash": self._user_hash,
                "content-type": "application/json",
                "X-Cache-Bypass": "yes",
            },
        )

    async def __aenter__(self) -> "LalafoClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(
                    url, headers={"request-id": str(uuid.uuid4())}
                )
                if response.status_code in (403, 429):
                    if attempt >= self.max_retries:
                        raise LalafoAccessError(
                            f"Lalafo returned HTTP {response.status_code}; access was not bypassed"
                        )
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    await asyncio.sleep(max(retry_after, 2**attempt))
                    continue
                if response.status_code in (404, 410):
                    raise LalafoNotFound("Lalafo advertisement is unavailable")
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "Temporary Lalafo error", request=response.request, response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise LalafoError("Lalafo JSON response is not an object")
                return payload
            except LalafoNotFound:
                raise
            except LalafoAccessError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(2**attempt)
        raise LalafoError(f"Lalafo request failed after retries: {type(last_error).__name__}")

    async def _get_text(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(
                    url,
                    headers={
                        "request-id": str(uuid.uuid4()),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                if response.status_code in (403, 429):
                    if attempt >= self.max_retries:
                        raise LalafoAccessError(
                            f"Lalafo returned HTTP {response.status_code}; access was not bypassed"
                        )
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    await asyncio.sleep(max(retry_after, 2**attempt))
                    continue
                if response.status_code in (404, 410):
                    raise LalafoNotFound("Lalafo advertisement is unavailable")
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "Temporary Lalafo error", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.text
            except (LalafoNotFound, LalafoAccessError):
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(2**attempt)
        raise LalafoError(f"Lalafo request failed after retries: {type(last_error).__name__}")

    @staticmethod
    def _search_params(
        search_url: str, page: int, offerer: str | None = None
    ) -> list[tuple[str, str]]:
        """Translate the configured human URL to Lalafo's public feed filters."""
        parts = urlsplit(search_url)
        path_parts = {part for part in parts.path.split("/") if part}
        room_ids = {
            "studio": "15496",
            "1-bedroom": "2773",
            "2-bedrooms": "2774",
        }
        params: list[tuple[str, str]] = [
            ("expand", "url"),
            ("per-page", "20"),
            ("category_id", "2044"),
            ("page", str(page)),
            ("city_id", "103184"),
        ]
        for index, (_, value) in enumerate(
            (item for item in room_ids.items() if item[0] in path_parts)
        ):
            params.append((f"parameters[69][{index}]", value))
        if "bez-podseleniya" in path_parts:
            params.append(("parameters[946][0]", "81537"))
        if offerer == "owner":
            params.append(("parameters[2149][0]", "19057"))
        elif offerer == "realtor":
            params.append(("parameters[2149][0]", "42340"))
        else:
            offerer_index = 0
            if "owner" in path_parts:
                params.append((f"parameters[2149][{offerer_index}]", "19057"))
                offerer_index += 1
            if "real-estate-agency" in path_parts:
                params.append((f"parameters[2149][{offerer_index}]", "42340"))
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key in {"price[from]", "price[to]", "currency", "sort_by"}:
                params.append((key, value))
        params.append(("with_feed_banner", "true"))
        return params

    async def search(
        self, search_url: str, page: int = 1, offerer: str | None = None
    ):
        parts = urlsplit(search_url)
        api_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                "/api/search/v3/feed/search",
                urlencode(self._search_params(search_url, page, offerer)),
                "",
            )
        )
        data = await self._get_json(api_url)
        return parse_search_data(data, base_url=f"{parts.scheme}://{parts.netloc}")

    async def detail(self, detail_url: str):
        match = re.search(r"-id-(\d+)(?:$|[/?#])", detail_url)
        if not match:
            raise LalafoError("Lalafo detail URL has no advertisement id")
        expected_id = int(match.group(1))
        html = await self._get_text(detail_url)
        ad = parse_detail_page(html, source_url=detail_url)
        if ad.lalafo_id != expected_id:
            raise LalafoError(
                f"Lalafo detail mismatch: expected {expected_id}, received {ad.lalafo_id}"
            )
        return ad

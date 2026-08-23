from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.lalafo.parser import parse_detail_page, parse_search_page

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
            },
        )

    async def __aenter__(self) -> "LalafoClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.get(url)
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

    async def search(self, search_url: str, page: int = 1):
        parts = urlsplit(search_url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params["page"] = str(page)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
        html = await self._get(url)
        return parse_search_page(html)

    async def detail(self, detail_url: str):
        html = await self._get(detail_url)
        return parse_detail_page(html, source_url=detail_url)

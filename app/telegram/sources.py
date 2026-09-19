from __future__ import annotations

import re

import httpx

LALAFO_URL_RE = re.compile(r"https?://(?:www\.)?lalafo\.kg/[\w./?=&%-]+", re.I)


async def fetch_lalafo_urls(
    channels: tuple[str, ...], *, timeout: float = 20.0, limit: int = 80
) -> list[str]:
    """Read public Telegram previews and return unique Lalafo ad URLs."""
    found: list[str] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as http:
        for channel in channels:
            try:
                response = await http.get(channel)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            for raw_url in LALAFO_URL_RE.findall(response.text):
                url = raw_url.rstrip(".,);]\\\"'")
                match = re.search(r"-id-(\d+)", url)
                if not match:
                    continue
                canonical = f"https://lalafo.kg/bishkek/ads/{url.rsplit('/', 1)[-1]}"
                if canonical in seen:
                    continue
                seen.add(canonical)
                found.append(canonical)
                if len(found) >= limit:
                    return found
    return found

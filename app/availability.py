from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.lalafo.client import LalafoClient, LalafoNotFound
from app.payments.repository import ApartmentRepository


_RENTED = re.compile(r"(?iu)\b(?:сдан[аоы]?|снят[аоы]?|неактуальн\w*)\b")
_TAGS = re.compile(r"<[^>]+>")
_BISHKEK = ZoneInfo("Asia/Bishkek")
AVAILABILITY_CACHE_HOURS = 12
MAX_LISTING_AGE_DAYS = 2


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    status: str
    reason: str
    checked_at: datetime
    cached: bool = False

    @property
    def message(self) -> str:
        stamp = self.checked_at.astimezone(_BISHKEK).strftime("%d.%m.%Y %H:%M")
        if self.status == "active":
            return f"Объявление актуально.\nПоследняя проверка: {stamp}."
        if self.status == "unknown":
            return "Пока не удалось проверить объявление. Попробуйте ещё раз позже."
        return f"Объявление не актуально.\nПоследняя проверка: {stamp}."


class AvailabilityService:
    def __init__(self, apartments: ApartmentRepository, settings: Settings) -> None:
        self.apartments = apartments
        self.settings = settings
        self._cache: dict[int, tuple[datetime, AvailabilityResult]] = {}

    async def check(self, apartment_id: int, *, force: bool = False) -> AvailabilityResult:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(apartment_id)
        if not force and cached and now < cached[0]:
            result = cached[1]
            return AvailabilityResult(result.status, result.reason, result.checked_at, True)
        self._cache.pop(apartment_id, None)
        apartment = await self.apartments.get(apartment_id)
        if apartment is None:
            return AvailabilityResult("unavailable", "missing", datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        published_at = apartment.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if published_at:
            # A newly published card receives one complete 48-hour window.
            # Reposts must not inherit an old removal check from an earlier card.
            deadline = published_at + timedelta(days=MAX_LISTING_AGE_DAYS)
            status = "active" if now < deadline else "unavailable"
            reason = "publication_age_window" if status == "active" else "listing_age_limit"
            if force or apartment.availability_status != status or apartment.active != (status == "active"):
                await self.apartments.set_availability(
                    apartment_id, status=status, checked_at=now, reason=reason,
                )
            result = AvailabilityResult(status, reason, now)
            expires = now + timedelta(seconds=30)
            if status == "active":
                expires = min(expires, deadline)
            if len(self._cache) >= 512:
                self._cache.pop(next(iter(self._cache)))
            self._cache[apartment_id] = (expires, result)
            return result
        checked = apartment.availability_checked_at
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        cache_age = (timedelta(minutes=1) if apartment.availability_status == "unknown"
                     else timedelta(hours=AVAILABILITY_CACHE_HOURS))
        if not force and checked and now - checked < cache_age:
            return AvailabilityResult(
                apartment.availability_status, apartment.availability_reason or "cached", checked, True
            )

        url = apartment.source_url
        if "lalafo.kg/" in url:
            status, reason = await self._check_lalafo(url)
        elif url.startswith("https://t.me/"):
            status, reason = await self._check_telegram(url)
        else:
            status, reason = "unknown", "source_not_checkable"
        await self.apartments.set_availability(
            apartment_id, status=status, checked_at=now, reason=reason
        )
        return AvailabilityResult(status, reason, now)

    async def _check_lalafo(self, url: str) -> tuple[str, str]:
        try:
            async with LalafoClient(
                timeout=min(6, self.settings.http_timeout_seconds),
                max_retries=0,
                proxy_url=self.settings.lalafo_proxy_url,
            ) as client:
                async with asyncio.timeout(8):
                    await client.detail(url)
            return "active", "source_available"
        except LalafoNotFound:
            return "unavailable", "source_removed"
        except Exception:
            return "unknown", "source_temporarily_unavailable"

    async def _check_telegram(self, url: str) -> tuple[str, str]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=7) as client:
                response = await client.get(url, params={"embed": "1"})
            if response.status_code == 404:
                return "unavailable", "source_removed"
            if response.status_code >= 400:
                return "unknown", "source_temporarily_unavailable"
            plain = html.unescape(_TAGS.sub(" ", response.text))
            if _RENTED.search(plain):
                return "unavailable", "marked_unavailable"
            if "tgme_widget_message" not in response.text:
                return "unavailable", "source_removed"
            return "active", "source_available"
        except (httpx.HTTPError, TimeoutError):
            return "unknown", "source_temporarily_unavailable"

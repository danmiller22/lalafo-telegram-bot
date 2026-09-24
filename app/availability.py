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
            return (
                f"Объявление доступно на источнике. Проверено {stamp}. "
                "Окончательную доступность квартиры подтвердите по телефону."
            )
        if self.status == "unavailable":
            return f"Квартира больше недоступна. Проверено {stamp}."
        return f"Не удалось проверить, попробуйте позже. Проверено {stamp}."


class AvailabilityService:
    def __init__(self, apartments: ApartmentRepository, settings: Settings) -> None:
        self.apartments = apartments
        self.settings = settings

    async def check(self, apartment_id: int, *, force: bool = False) -> AvailabilityResult:
        apartment = await self.apartments.get(apartment_id)
        if apartment is None:
            return AvailabilityResult("unavailable", "missing", datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        checked = apartment.availability_checked_at
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if not force and checked and now - checked < timedelta(minutes=5):
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

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.lalafo.models import LalafoAd
from app.telegram.source_filter import is_owner_offer


logger = logging.getLogger(__name__)
CHANNEL_REQUEST_ATTEMPTS = 2
CHANNEL_CONCURRENCY = 4

LALAFO_URL_RE = re.compile(r"https?://(?:www\.)?lalafo\.kg/[\w./?=&%-]+", re.I)
PHOTO_URL_RE = re.compile(r"background-image:url\(['\"]?([^'\")]+)", re.I)
ROOM_RE = re.compile(
    r"\b([12])\s*(?:[-‑–—хx]\s*)?"
    r"(?:комнат(?:ная|ную|ные|ы)?|ком(?:н(?:ат)?)?\.?|б[өо]лм[өо]л[үүу]+)",
    re.I,
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?996[\s()\-]*|0)([2579]\d{2}(?:[\s()\-]*\d){6})(?!\d)"
)
PRICE_PATTERNS = (
    re.compile(
        r"(?:аренд(?:а|ная\s+плата)|оплата|цена|стоимость(?:\s+аренды)?|квартплата|баасы)"
        r"\s*[:—–+\-]?\s*(?:от\s*)?(\d{1,3}(?:[ .]\d{3})|\d{4,6})"
        r"(?:\s*(?:сом|с))?\b",
        re.I,
    ),
    re.compile(
        r"(?<!\d)(\d{1,3}(?:[ .]\d{3})|\d{4,6})\s*(?:сом|с|kgs|кгс)\b",
        re.I,
    ),
    re.compile(
        r"(?<!\d)(\d{1,3}(?:[ .]\d{3})|\d{4,6})\s*"
        r"(?:сом(?:ов)?|с)?\s*(?:в\s+месяц|оплата|аренда|цена|квартплата)\b",
        re.I,
    ),
)
THOUSANDS_PRICE_PATTERNS = (
    re.compile(
        r"(?:аренд(?:а|ная\s+плата)|оплата|цена|стоимость(?:\s+аренды)?|квартплата|баасы)"
        r"\s*[:—–+\-]?\s*(?:от\s*)?(\d{1,3}(?:[.,]\d+)?)\s*тыс(?:яч[аи]?)?\.?\b",
        re.I,
    ),
    re.compile(
        r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*тыс(?:яч[аи]?)?\.?\s*"
        r"(?:сом(?:ов)?|с)?\s*(?:в\s+месяц|оплата|аренда|цена|квартплата)\b",
        re.I,
    ),
)
FOREIGN_RENT_RE = re.compile(
    r"(?:аренд(?:а|ная\s+плата)|оплата|цена|стоимость|квартплата|💰)"
    r".{0,28}?(?:\$|доллар|usd)",
    re.I,
)
FOREIGN_KGS_EQUIVALENT_RE = re.compile(
    r"\((\d{1,3}(?:[ .]\d{3})|\d{4,6})\s*(?:сом|kgs|кгс)\)",
    re.I,
)
DEPOSIT_RE = re.compile(
    r"депозит\s*[:—–+\-]?\s*(\d{1,3}(?:[ .]\d{3})|\d{4,6})\s*(?:сом|с)?\b",
    re.I,
)
SEARCH_TERMS = (
    "ищу квартиру",
    "ищем квартиру",
    "сниму квартиру",
    "снимем квартиру",
    "нужна квартира",
    "нужен квартира",
)
SHARED_HOUSING_TERMS = (
    "подсел",
    "койко-мест",
    "общежит",
    "хостел",
)
REALTOR_TERMS = ("риелтор", "риэлтор", "агентство", "комиссия")
OFFER_TERMS = (
    "сдаётся",
    "сдается",
    "сдаю",
    "в аренду",
    "аренда, квартира",
    "аренда квартира",
    "ижарага берилет",
    "арендага берилет",
    "квартира берилет",
    "батир берилет",
)


async def fetch_lalafo_urls(
    channels: tuple[str, ...], *, timeout: float = 20.0, limit: int = 80
) -> list[str]:
    """Read public Telegram previews and return unique Lalafo ad URLs."""
    found: list[str] = []
    seen: set[str] = set()
    semaphore = asyncio.Semaphore(CHANNEL_CONCURRENCY)

    async def fetch_channel(http: httpx.AsyncClient, channel: str) -> str:
        for attempt in range(CHANNEL_REQUEST_ATTEMPTS):
            try:
                async with semaphore:
                    response = await http.get(channel)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                if attempt + 1 < CHANNEL_REQUEST_ATTEMPTS:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning("Telegram link source failed channel=%s error=%s", channel, type(exc).__name__)
        return ""

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as http:
        pages = await asyncio.gather(
            *(fetch_channel(http, channel) for channel in channels)
        )
        for page in pages:
            for raw_url in LALAFO_URL_RE.findall(page):
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


def _integer(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    return int(re.sub(r"\D", "", match.group(1)))


def _telegram_price(text: str) -> int | None:
    if FOREIGN_RENT_RE.search(text):
        return _integer(FOREIGN_KGS_EQUIVALENT_RE.search(text))
    for pattern in PRICE_PATTERNS:
        value = _integer(pattern.search(text))
        if value is not None and 5_000 <= value <= 500_000:
            return value
    for pattern in THOUSANDS_PRICE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        value = round(float(match.group(1).replace(",", ".")) * 1_000)
        if 5_000 <= value <= 500_000:
            return value
    return None


def _telegram_phone(text: str) -> str | None:
    match = PHONE_RE.search(text)
    if match is None:
        return None
    local = re.sub(r"\D", "", match.group(1))
    return f"+996{local}" if len(local) == 9 else None


def _telegram_rooms(text: str) -> str | None:
    if re.search(r"\bстуди(?:я|ю|и)\b", text, re.I):
        return "studio"
    match = ROOM_RE.search(text)
    if match is not None:
        return match.group(1)
    if re.search(r"\bевро[- ]?двушк", text, re.I):
        return "2"
    return None


def _telegram_district(lines: list[str], text: str) -> str | None:
    labels = ("район", "адрес", "дареги", "локация", "микрорайон", "мкр")
    for index, raw_line in enumerate(lines):
        line = re.sub(r"^[^\wА-Яа-яЁё]+", "", raw_line).strip()
        normalized = line.casefold().replace("ё", "е")
        if not any(normalized.startswith(label) for label in labels):
            continue
        value = re.sub(
            r"^(?:район|адрес|дареги|локация|микрорайон|мкр\.?)\s*[:—–-]?\s*",
            "",
            line,
            flags=re.I,
        ).strip(" .,:;—–-")
        if not value and index + 1 < len(lines):
            value = lines[index + 1].strip(" .,:;—–-")
        if 2 <= len(value) <= 80:
            return value
    title = lines[0] if lines else text
    if "|" in title:
        value = title.rsplit("|", 1)[-1].strip()
        if 2 <= len(value) <= 80:
            return value
    return None


def _telegram_source_id(data_post: str) -> int:
    digest = hashlib.blake2b(data_post.encode("utf-8"), digest_size=8).digest()
    return -(int.from_bytes(digest, "big") & ((1 << 63) - 1))


def parse_telegram_apartments(
    html: str,
    *,
    now: datetime | None = None,
    max_age_hours: int = 96,
) -> list[LalafoAd]:
    """Parse fresh, photo-backed whole-apartment offers from a public channel."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=max(1, max_age_hours))
    soup = BeautifulSoup(html, "html.parser")
    ads: list[LalafoAd] = []
    for wrapper in soup.select(".tgme_widget_message_wrap"):
        message = wrapper.select_one(".tgme_widget_message[data-post]")
        text_element = wrapper.select_one(".tgme_widget_message_text")
        time_element = wrapper.select_one("time[datetime]")
        if message is None or text_element is None or time_element is None:
            continue
        try:
            created_at = datetime.fromisoformat(str(time_element.get("datetime")))
        except ValueError:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at.astimezone(timezone.utc) < cutoff:
            continue

        lines = [
            line.strip()
            for line in text_element.get_text("\n", strip=True).splitlines()
            if line.strip()
        ]
        text = " ".join(lines)
        normalized = text.casefold().replace("ё", "е")
        if not any(term in normalized for term in OFFER_TERMS):
            continue
        if any(term in normalized for term in SEARCH_TERMS):
            continue
        if any(term in normalized for term in SHARED_HOUSING_TERMS):
            continue
        if re.search(r"(?:сда[её]тся|сдаю)\s+(?:отдельная\s+)?комната\b", text, re.I):
            continue
        if any(term in normalized for term in ("частный дом", "сдается дом", "сдаётся дом", "пол дома")):
            continue

        price = _telegram_price(text)
        rooms = _telegram_rooms(text)
        phone = _telegram_phone(text)
        if price is None or not 20_000 <= price <= 40_000 or rooms is None or phone is None:
            continue
        photo_urls: list[str] = []
        for photo in wrapper.select(".tgme_widget_message_photo_wrap[style]"):
            match = PHOTO_URL_RE.search(str(photo.get("style") or ""))
            if match is not None and match.group(1) not in photo_urls:
                photo_urls.append(match.group(1))
        if not photo_urls:
            continue

        data_post = str(message.get("data-post"))
        owner = is_owner_offer(text)
        seller_type = (
            "owner"
            if owner
            else "realtor"
            if any(term in normalized for term in REALTOR_TERMS)
            else "unknown"
        )
        ads.append(
            LalafoAd(
                lalafo_id=_telegram_source_id(data_post),
                source_url=f"https://t.me/{data_post}",
                phone=phone,
                price=price,
                currency="KGS",
                rooms=rooms,
                district=_telegram_district(lines, text),
                city="Бишкек",
                deposit=_integer(DEPOSIT_RE.search(text)),
                photo_urls=photo_urls,
                category_id=2044,
                no_subletting=True,
                owner_listing=owner,
                seller_type=seller_type,
                source_title=lines[0] if lines else "Квартира",
                source_description=text,
                source_created_at=created_at,
                source_updated_at=created_at,
            )
        )
    return ads


async def fetch_telegram_apartments(
    channels: tuple[str, ...],
    *,
    timeout: float = 20.0,
    limit: int = 120,
    pages_per_channel: int = 12,
    max_age_hours: int = 168,
) -> list[LalafoAd]:
    """Fetch several recent public preview pages from each approved channel."""

    semaphore = asyncio.Semaphore(CHANNEL_CONCURRENCY)

    async def fetch_channel(http: httpx.AsyncClient, channel: str) -> list[LalafoAd]:
        parsed = urlparse(channel)
        handle = parsed.path.strip("/").removeprefix("s/")
        if not handle:
            return []
        base = f"https://t.me/s/{handle}"
        before: int | None = None
        found: list[LalafoAd] = []
        seen_before: set[int] = set()
        for _ in range(max(1, pages_per_channel)):
            url = base if before is None else f"{base}?before={before}"
            response: httpx.Response | None = None
            for attempt in range(CHANNEL_REQUEST_ATTEMPTS):
                try:
                    async with semaphore:
                        response = await http.get(url)
                    response.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    response = None
                    if attempt + 1 < CHANNEL_REQUEST_ATTEMPTS:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    logger.warning(
                        "Telegram apartment source failed channel=%s error=%s",
                        channel,
                        type(exc).__name__,
                    )
            if response is None:
                break
            try:
                found.extend(
                    parse_telegram_apartments(
                        response.text,
                        max_age_hours=max_age_hours,
                    )
                )
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Telegram apartment source parse failed channel=%s error=%s",
                    channel,
                    type(exc).__name__,
                )
                break
            soup = BeautifulSoup(response.text, "html.parser")
            message_ids = []
            for message in soup.select(".tgme_widget_message[data-post]"):
                try:
                    message_ids.append(int(str(message.get("data-post")).rsplit("/", 1)[-1]))
                except ValueError:
                    continue
            if not message_ids:
                break
            next_before = min(message_ids)
            if next_before in seen_before:
                break
            seen_before.add(next_before)
            before = next_before
            timestamps = []
            for item in soup.select("time[datetime]"):
                try:
                    timestamps.append(datetime.fromisoformat(str(item.get("datetime"))))
                except ValueError:
                    continue
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            if timestamps and all(
                (value if value.tzinfo else value.replace(tzinfo=timezone.utc)) < cutoff
                for value in timestamps
            ):
                break
        return found

    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as http:
        batches = await asyncio.gather(
            *(fetch_channel(http, channel) for channel in channels),
            return_exceptions=True,
        )
    unique: dict[int, LalafoAd] = {}
    for channel, batch in zip(channels, batches):
        if isinstance(batch, BaseException):
            logger.warning(
                "Telegram apartment source isolated channel=%s error=%s",
                channel,
                type(batch).__name__,
            )
            continue
        for ad in batch:
            unique.setdefault(ad.lalafo_id, ad)
        logger.info(
            "Telegram apartment source parsed channel=%s eligible=%d",
            channel,
            len(batch),
        )
    return sorted(
        unique.values(),
        key=lambda ad: ad.source_updated_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[: max(1, limit)]

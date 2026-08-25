from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from app.lalafo.deposit import parse_deposit
from app.lalafo.models import LalafoAd, SearchAd, SearchPage
from app.lalafo.phone import normalize_kg_phone


class LalafoParseError(ValueError):
    pass


def _normalized_listing_text(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def _is_without_subletting(raw: dict[str, Any], params: dict[str, Any]) -> bool:
    """Infer whether the whole apartment is offered rather than shared housing.

    Lalafo does not require authors to select either subletting option. Missing
    metadata therefore means a regular whole-apartment rental. Some authors
    select both options; in that contradictory case the explicit shared-housing
    option wins so the card never promises a private apartment incorrectly.
    """
    audience = _normalized_listing_text(params.get("Для кого"))
    if "с подселением" in audience:
        return False
    if "без подселения" in audience:
        return True

    listing_text = _normalized_listing_text(
        " ".join((str(raw.get("title") or ""), str(raw.get("description") or "")))
    )
    shared_markers = (
        "с подселением",
        "койко место",
        "койкоместо",
        "место в комнате",
        "подселим",
        "подселю",
    )
    if any(marker in listing_text for marker in shared_markers):
        return False
    return True


def _next_data(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        raise LalafoParseError("Lalafo __NEXT_DATA__ was not found")
    try:
        payload = json.loads(script.string)
    except json.JSONDecodeError as exc:
        raise LalafoParseError("Invalid Lalafo __NEXT_DATA__ JSON") from exc
    return payload


def _queries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return payload["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as exc:
        raise LalafoParseError("Unexpected Lalafo Next.js payload") from exc


def _query_data(payload: dict[str, Any], prefix: str) -> Any:
    for query in _queries(payload):
        key = query.get("queryKey")
        first = key[0] if isinstance(key, list) and key else key
        if first == prefix:
            return query.get("state", {}).get("data")
    raise LalafoParseError(f"Lalafo query {prefix!r} was not found")


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _image_urls(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for image in item.get("images") or []:
        url = image.get("original_url") or image.get("original_webp_url")
        if url:
            result.append(str(url))
    return result


def parse_search_data(page: dict[str, Any], *, base_url: str = "https://lalafo.kg") -> SearchPage:
    """Parse one response from Lalafo's first-party public feed endpoint."""
    if not isinstance(page, dict):
        raise LalafoParseError("Lalafo feed payload is not an object")
    items: list[SearchAd] = []
    for raw in page.get("items") or []:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("url"):
            continue
        url = str(raw["url"])
        if url.startswith("/"):
            url = base_url.rstrip("/") + url
        items.append(
            SearchAd(
                lalafo_id=int(raw["id"]),
                detail_url=url,
                price=raw.get("price"),
                currency=raw.get("currency"),
                city=raw.get("city"),
                photo_urls=_image_urls(raw),
                updated_at=_timestamp(raw.get("updated_time")),
            )
        )
    meta = page.get("_meta") or {}
    return SearchPage(
        items=items,
        total=int(meta.get("totalCount") or meta.get("total") or len(items)),
        current_page=int(meta.get("currentPage") or meta.get("current_page") or 1),
        page_count=int(
            meta.get("pageCount") or meta.get("page_count") or meta.get("last_page") or 1
        ),
    )


def parse_search_page(html: str, *, base_url: str = "https://lalafo.kg") -> SearchPage:
    """Compatibility parser for server-rendered Next.js pages."""
    payload = _next_data(html)
    data = _query_data(payload, "listingFeed")
    pages = (data or {}).get("pages") or []
    if not pages:
        raise LalafoParseError("Lalafo listingFeed has no pages")
    return parse_search_data(pages[0], base_url=base_url)


def parse_detail_data(raw: dict[str, Any], *, source_url: str) -> LalafoAd:
    """Parse one response from Lalafo's first-party public details endpoint."""
    if not isinstance(raw, dict) or not raw.get("id"):
        raise LalafoParseError("Lalafo detail payload is empty")
    if raw.get("hide_phone"):
        raise LalafoParseError("Advertisement owner hides the phone number")
    params = {str(item.get("name", "")).strip(): item.get("value") for item in raw.get("params") or []}
    rooms_value = str(params.get("Количество комнат") or "").strip().lower()
    room_map = {"студия": "studio", "1 комната": "1", "2 комнаты": "2"}
    rooms = room_map.get(rooms_value, rooms_value)
    district = str(params.get("Район Бишкека") or "").strip() or None
    deposit_value = params.get("Депозит, сом")
    try:
        deposit = int(str(deposit_value).replace(" ", "")) if deposit_value else None
    except ValueError:
        deposit = None
    if deposit is None:
        deposit = parse_deposit(raw.get("description"))
    if deposit == 1:
        # Lalafo authors commonly use 1 som as a placeholder rather than a real deposit.
        deposit = None
    offerer = str(params.get("Кто предлагает") or "").strip().casefold()
    realtor_service = str(params.get("Услуги риэлтора") or "").strip()
    return LalafoAd(
        lalafo_id=int(raw["id"]),
        source_url=source_url,
        phone=normalize_kg_phone(raw.get("mobile")),
        price=int(raw.get("price") or 0),
        currency=str(raw.get("currency") or ""),
        rooms=rooms,
        district=district,
        city=str(raw.get("city") or ""),
        deposit=deposit,
        photo_urls=_image_urls(raw),
        category_id=int(raw.get("category_id") or 0),
        no_subletting=_is_without_subletting(raw, params),
        owner_listing=offerer == "собственник" and not realtor_service,
        source_created_at=_timestamp(raw.get("created_time")),
        source_updated_at=_timestamp(raw.get("updated_time")),
    )


def parse_detail_page(html: str, *, source_url: str) -> LalafoAd:
    """Compatibility parser for server-rendered Next.js pages."""
    payload = _next_data(html)
    raw = _query_data(payload, "detail")
    return parse_detail_data(raw, source_url=source_url)


def is_allowed(ad: LalafoAd, *, city: str, max_price: int, rooms: tuple[str, ...]) -> tuple[bool, str]:
    if ad.category_id != 2044:
        return False, "wrong_category"
    if ad.city.casefold() != city.casefold():
        return False, "wrong_city"
    if ad.currency.upper() != "KGS":
        return False, "wrong_currency"
    if ad.price <= 0 or ad.price > max_price:
        return False, "price"
    if ad.rooms not in rooms:
        return False, "rooms"
    if not ad.photo_urls:
        return False, "photos"
    if not ad.phone:
        return False, "phone"
    return True, "ok"

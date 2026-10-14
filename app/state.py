from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.lalafo.models import LalafoAd


def normalized_district(value: str | None) -> str:
    district = (value or "").casefold().replace("ё", "е")
    district = re.sub(r"\b(?:мкр|микрорайон)\b", "", district)
    return re.sub(r"[^0-9a-zа-я]+", "", district)


def _photo_keys(urls: list[str]) -> set[str]:
    keys = set()
    for url in urls:
        name = urlsplit(url).path.rsplit("/", 1)[-1].casefold()
        stem = name.rsplit(".", 1)[0]
        if len(stem) >= 12:
            keys.add(stem)
    return keys


def same_listing(ad: LalafoAd, other: LalafoAd | object) -> bool:
    """Match one apartment across source IDs and minor district variations."""
    if ad.lalafo_id == getattr(other, "lalafo_id", None):
        return True
    if ad.rooms != getattr(other, "rooms", None):
        return False
    ad_district = normalized_district(ad.district)
    other_district = normalized_district(getattr(other, "district", None))
    if not ad_district or ad_district != other_district:
        return False
    other_price = getattr(other, "price", None)
    if not isinstance(other_price, int):
        return False
    same_contact = (
        ad.phone == getattr(other, "phone", None)
        and ad.price == other_price
    )
    other_photos = getattr(other, "photo_urls", None) or []
    if same_contact and (not ad.photo_urls or not other_photos
                         or set(ad.photo_urls) == set(other_photos)):
        return True
    if abs(ad.price - other_price) > max(ad.price, other_price) * 0.2:
        return False
    ad_photos = _photo_keys(ad.photo_urls)
    existing_photos = _photo_keys(other_photos)
    common = ad_photos & existing_photos
    # Reserve feeds can expose only the cover of an otherwise complete album.
    # One identical cover is sufficient only for a single-photo card with the
    # same room count, district and price; a shared image in two larger albums
    # must still meet the stronger two-photo rule.
    return len(common) >= 2 or (
        len(common) == 1
        and min(len(ad_photos), len(existing_photos)) == 1
        and ad.price == other_price
    )


class ListingIndex:
    """Index published identities once instead of comparing every pair of ads."""

    def __init__(self, listings: Iterable[object]) -> None:
        self.ids: set[int] = set()
        self.contacts: dict[tuple, list[frozenset[str]]] = defaultdict(list)
        self.photos: dict[tuple, list[tuple[int, int, int]]] = defaultdict(list)
        for item in listings:
            self.ids.add(item.lalafo_id)
            district = normalized_district(item.district)
            if not district or not isinstance(item.price, int):
                continue
            urls = item.photo_urls or []
            self.contacts[(item.rooms, district, item.phone, item.price)].append(frozenset(urls))
            keys = _photo_keys(urls)
            for photo in keys:
                self.photos[(item.rooms, district, photo)].append((item.lalafo_id, item.price, len(keys)))

    def contains(self, ad: LalafoAd) -> bool:
        if ad.lalafo_id in self.ids:
            return True
        district = normalized_district(ad.district)
        if not district:
            return False
        urls = frozenset(ad.photo_urls)
        contact = (ad.rooms, district, ad.phone, ad.price)
        if any(not urls or not other or urls == other for other in self.contacts.get(contact, ())):
            return True
        overlaps: Counter[int] = Counter()
        keys = _photo_keys(ad.photo_urls)
        for photo in keys:
            for listing_id, price, photo_count in self.photos.get((ad.rooms, district, photo), ()):
                if abs(ad.price - price) <= max(ad.price, price) * 0.2:
                    if min(len(keys), photo_count) == 1 and ad.price == price:
                        return True
                    overlaps[listing_id] += 1
                    if overlaps[listing_id] >= 2:
                        return True
        return False


def ad_fingerprint(ad: LalafoAd) -> str:
    # Agents can offer several different apartments at the same price in one
    # district. Their contact identifies the seller, not the apartment.
    photos = sorted(_photo_keys(ad.photo_urls) or {
        urlsplit(url).path for url in ad.photo_urls
    })
    raw = "|".join((ad.phone, str(ad.price), normalized_district(ad.district),
                    ad.rooms, *photos))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class PostedState:
    path: Path
    items: list[dict[str, object]]

    @classmethod
    def load(cls, path: Path) -> "PostedState":
        if not path.exists():
            return cls(path=path, items=[])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError(f"State file is invalid: {path}")
        return cls(path=path, items=list(payload.get("items") or []))

    def contains(self, lalafo_id: int, fingerprint: str | None = None) -> bool:
        return any(
            int(item.get("lalafo_id") or 0) == lalafo_id
            or (fingerprint and item.get("fingerprint") == fingerprint)
            for item in self.items
        )

    def add(self, ad: LalafoAd, *, telegram_message_id: int) -> None:
        self.items.append(
            {
                "lalafo_id": ad.lalafo_id,
                "fingerprint": ad_fingerprint(ad),
                "published_at": datetime.now(timezone.utc).isoformat(),
                "telegram_message_id": telegram_message_id,
            }
        )

    def prune(self, retention_days: int) -> None:
        threshold = datetime.now(timezone.utc) - timedelta(days=retention_days)
        kept: list[dict[str, object]] = []
        for item in self.items:
            try:
                published = datetime.fromisoformat(str(item["published_at"]))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if published >= threshold:
                kept.append(item)
        self.items = kept

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": self.items,
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


PHONE_SOURCE_VERSION = 2


class SearchAd(BaseModel):
    lalafo_id: int
    detail_url: str
    price: int | None = None
    currency: str | None = None
    city: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class SearchPage(BaseModel):
    items: list[SearchAd]
    total: int = 0
    current_page: int = 1
    page_count: int = 1


class LalafoAd(BaseModel):
    lalafo_id: int
    source_url: str
    phone: str
    price: int
    currency: str
    rooms: str
    district: str | None = None
    city: str
    deposit: int | None = None
    photo_urls: list[str]
    category_id: int
    no_subletting: bool
    owner_listing: bool
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.inventory import BISHKEK, InventoryRepository, as_utc
from app.models import Apartment, ApartmentInventoryQueue
from app.shared_housing import SHARED_DAILY_TARGET, valid_shared
from app.telegram.formatting import format_apartment
from app.telegram.sources import parse_telegram_apartments
from scripts.publish_inventory import _valid
from tests.helpers import make_ad
from tests.test_telegram_sources import _post


def shared_ad(index=1, **kwargs):
    return make_ad(lalafo_id=-900000-index, source_url=f"https://t.me/shared_test/{index}",
        rooms="shared", price=12_000, no_subletting=False, owner_listing=False,
        seller_type="unknown", source_description="Нужна девушка на подселение. Цена 12000 сом.",
        phone=f"+996700{index:06d}", photo_urls=[f"https://img.example/shared-{index}.jpg"]).model_copy(update=kwargs)


def test_shared_posts_keep_their_kind_price_and_contact_without_owner_invention():
    text = "Подселение для девушек | Юг-2<br>Цена 12000 сом.<br>0705 123 456"
    ads = parse_telegram_apartments(_post(text), now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc))
    assert len(ads) == 1
    ad = ads[0]
    assert ad.rooms == "shared" and ad.price == 12_000 and not ad.no_subletting
    assert not ad.owner_listing and ad.seller_type == "unknown"
    assert _valid(ad, Settings()) == (True, "ok")
    caption = format_apartment(ad)
    assert "Подселение" in caption and "Совместное проживание" in caption
    assert "1-комнатная квартира" not in caption


@pytest.mark.parametrize("text", [
    "Ищу квартиру с подселением. Цена 12000 сом. 0705 123 456",
    "Подселение в хостел. Цена 12000 сом. 0705 123 456",
    "Подселение в общежитие. Цена 12000 сом. 0705 123 456",
])
def test_shared_wanted_ads_and_lodging_are_rejected(text):
    assert not parse_telegram_apartments(_post(text), now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc))


def test_shared_exception_never_opens_lalafo_or_whole_apartment_price_rules():
    ad = shared_ad()
    assert valid_shared(ad)[0]
    assert not valid_shared(ad.model_copy(update={"source_url": "https://lalafo.kg/bishkek/ads/test-id-1"}))[0]
    assert not _valid(ad.model_copy(update={"rooms": "1", "no_subletting": True}), Settings())[0]
    assert not valid_shared(ad.model_copy(update={"photo_urls": []}))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_shared_stock_is_excluded_from_new_whole_apartment_plan(repositories, owner):
    apartments, _, sessions = repositories
    now = datetime.now(BISHKEK).replace(hour=5, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    for i in range(15):
        await apartments.upsert_discovered(shared_ad(i+1, price=25_000 if owner else 12_000,
            owner_listing=owner, seller_type="owner" if owner else "unknown"))
    inventory = InventoryRepository(sessions)
    assert await inventory.schedule_period(now=now) == 0
    assert await inventory.schedule_period(now=now) == 0
    async with sessions() as s:
        rows = list((await s.scalars(select(ApartmentInventoryQueue).order_by(ApartmentInventoryQueue.scheduled_at))).all())
    assert len(rows) == 0
    assert all(as_utc(b.scheduled_at)-as_utc(a.scheduled_at) >= timedelta(hours=2) for a,b in zip(rows,rows[1:]))


@pytest.mark.asyncio
async def test_shared_collector_stores_unknown_seller_without_relaxing_whole_policy(repositories, monkeypatch):
    from scripts.collect_telegram_inventory import collect
    apartments, _, sessions = repositories
    monkeypatch.setattr("scripts.collect_telegram_inventory.fetch_telegram_apartments", AsyncMock(return_value=[shared_ad()]))
    result = await collect(Settings(), apartments, InventoryRepository(sessions))
    assert result["stored"] == 1 and result["queued"] == 0


@pytest.mark.asyncio
async def test_catchup_cannot_cluster_shared_cards_and_requires_whole_card_between(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(BISHKEK).replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    first = await apartments.upsert_discovered(shared_ad(1))
    second = await apartments.upsert_discovered(shared_ad(2))
    async with sessions.begin() as s:
        await s.execute(update(Apartment).where(Apartment.id==first.id).values(publication_status="published", published_at=now-timedelta(minutes=30)))
        s.add(ApartmentInventoryQueue(apartment_id=second.id,window_key="shared:test",sequence=1,scheduled_at=now-timedelta(hours=3)))
    inventory=InventoryRepository(sessions)
    assert await inventory.claim_due(now=now) is None
    assert await inventory.claim_due(now=now+timedelta(hours=2)) is None
    whole=await apartments.upsert_discovered(make_ad(lalafo_id=900001))
    async with sessions.begin() as s:
        await s.execute(update(Apartment).where(Apartment.id==whole.id).values(publication_status="published", published_at=now+timedelta(minutes=30)))
    row=await inventory.claim_due(now=now+timedelta(hours=2))
    assert row is None


@pytest.mark.asyncio
async def test_daily_cap_survives_queue_rebuild(repositories):
    apartments, _, sessions = repositories
    now=datetime.now(BISHKEK).replace(hour=23, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    for i in range(10):
        ad=await apartments.upsert_discovered(shared_ad(i+1))
        async with sessions.begin() as s:
            await s.execute(update(Apartment).where(Apartment.id==ad.id).values(publication_status="published",published_at=now-timedelta(hours=3)))
    extra=await apartments.upsert_discovered(shared_ad(20))
    inventory=InventoryRepository(sessions)
    await inventory.reset_publication_history_for_code("shared-test-rebuild")
    async with sessions.begin() as s:
        s.add(ApartmentInventoryQueue(apartment_id=extra.id,window_key="shared:test",sequence=1,scheduled_at=now-timedelta(hours=1)))
    assert await inventory.claim_due(now=now) is None


@pytest.mark.asyncio
async def test_new_plan_keeps_uncertain_shared_receipt_for_reconciliation(repositories):
    apartments, _, sessions = repositories
    now = datetime.now(timezone.utc)
    ad = await apartments.upsert_discovered(shared_ad(99))
    async with sessions.begin() as session:
        session.add(ApartmentInventoryQueue(
            apartment_id=ad.id, window_key="shared:old", sequence=1,
            scheduled_at=now, status="uncertain", last_error="TelegramPublishAmbiguousError",
        ))
    await InventoryRepository(sessions).schedule_period(now=now)
    async with sessions() as session:
        row = await session.scalar(select(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.apartment_id == ad.id))
        assert row is not None and row.status == "uncertain"
        assert row.last_error == "TelegramPublishAmbiguousError"

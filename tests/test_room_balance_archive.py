from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from collections import Counter

import pytest
from sqlalchemy import select, update

from app.models import Apartment, ApartmentInventoryQueue
from app.inventory import plan_period, _balanced_stock, InventoryRepository, publication_day_start
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from app.config import Settings
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_expensive_one_room_is_not_crowded_out_by_many_cheap_two_rooms(repositories):
    apartments, _, _ = repositories
    rows = []
    for i in range(43):
        rows.append(await apartments.upsert_discovered(make_ad(lalafo_id=700000+i,
            rooms="1" if i < 3 else "2", price=40000 if i < 3 else 25000, district="ЦУМ")))
    now = publication_day_start(datetime.now(timezone.utc))
    planned = plan_period(rows, period_start=now, target_count_override=20)
    assert Counter(item.apartment.rooms for item in planned)["1"] == 3


@pytest.mark.asyncio
async def test_stock_loading_reserves_space_for_each_room_type(repositories, monkeypatch):
    from app import inventory
    apartments, _, sessions = repositories
    for i in range(10):
        await apartments.upsert_discovered(make_ad(lalafo_id=710000+i, rooms="2" if i < 6 else "1" if i < 8 else "studio"))
    monkeypatch.setattr(inventory, "MAX_FRESH_STOCK_LOAD", 6)
    async with sessions() as session:
        rows = await _balanced_stock(session, select(Apartment).order_by(Apartment.id))
    assert Counter(row.rooms for row in rows) == {"studio":2, "1":2, "2":2}


@pytest.mark.asyncio
async def test_dispatch_changes_room_type_after_a_two_room_card(repositories):
    apartments, _, sessions = repositories
    now = (publication_day_start(datetime.now(timezone.utc)) + timedelta(hours=2)).astimezone(timezone.utc)
    previous = await apartments.upsert_discovered(make_ad(lalafo_id=720000, rooms="2", district="ЦУМ"))
    await apartments.mark_published(previous.id, chat_id=-100, message_id=1, acknowledged_at=now-timedelta(minutes=11))
    one = await apartments.upsert_discovered(make_ad(lalafo_id=720001, rooms="1", price=40000, district="ЦУМ", phone="+996555000001", photo_urls=["https://img.example/one.jpg"]))
    two = await apartments.upsert_discovered(make_ad(lalafo_id=720002, rooms="2", price=25000, district="ЦУМ", phone="+996555000002", photo_urls=["https://img.example/two.jpg"]))
    async with sessions.begin() as session:
        for i, apartment in enumerate((two, one)):
            session.add(ApartmentInventoryQueue(apartment_id=apartment.id,
                scheduled_at=now-timedelta(minutes=1), window_key="ordinary", sequence=i+1))
    claimed = await InventoryRepository(sessions).claim_due(now=now)
    assert claimed is not None and claimed.apartment.id == one.id


@pytest.mark.asyncio
async def test_archive_card_has_clear_status_and_no_contact_checkout():
    bot = SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1)]),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)), send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
    publisher = TelegramPublisher(bot, chat_id=-100, signer=TokenSigner("archive-test-secret"), bot_username="testbot", support_url="https://t.me/support")
    await publisher.publish(1, make_ad(), archived=True)
    args = bot.send_message.await_args.kwargs
    assert "Архив" in args["text"] and "сдана" in args["text"]
    assert "Актуально на момент публикации" not in args["text"]
    assert all("access?" not in (button.url or "") for row in args["reply_markup"].inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_unavailable_one_room_is_published_as_archive_without_reactivation(repositories, service, monkeypatch):
    from scripts import publish_inventory as worker
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=730000, rooms="1"))
    await apartments.set_availability(apartment.id, status="unavailable", checked_at=datetime.now(timezone.utc), reason="rented")
    apartment = await apartments.get(apartment.id)
    item = SimpleNamespace(id=1, apartment=apartment, window_key="ordinary")
    queue = SimpleNamespace(finish_item=AsyncMock(), retry_item=AsyncMock())
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    sender = SimpleNamespace(publish=AsyncMock(return_value=SimpleNamespace(message_id=2)))
    monkeypatch.setattr(worker, "Bot", lambda **kwargs: bot)
    monkeypatch.setattr(worker, "TelegramPublisher", lambda *args, **kwargs: sender)
    detail = AsyncMock(side_effect=AssertionError("Archive must not claim fresh source verification"))
    monkeypatch.setattr(worker.LalafoClient, "detail", detail)
    result = await worker._publish_item(Settings(telegram_bot_token="123456:test", callback_secret="archive-test-secret"), sessions, queue, apartments, item)
    assert result == 0 and sender.publish.await_args.kwargs["archived"] is True
    stored = await apartments.get(apartment.id)
    assert stored.publication_status == "published" and not stored.active
    assert stored.availability_status == "unavailable"
    assert (await service.contact_status(123, apartment.id)).status == "unavailable"
    detail.assert_not_awaited()

from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.inventory import InventoryRepository
from scripts.collect_telegram_inventory import collect
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_telegram_inventory_is_stored_and_due_without_lalafo(repositories, monkeypatch):
    apartments, _, sessions = repositories
    ad = make_ad(lalafo_id=-100, source_url="https://t.me/bishkekarendakv/100")
    monkeypatch.setattr("scripts.collect_telegram_inventory.fetch_telegram_apartments", AsyncMock(return_value=[ad]))
    lalafo = AsyncMock(side_effect=AssertionError("Telegram collection must not call Lalafo"))
    monkeypatch.setattr("app.lalafo.client.LalafoClient.detail", lalafo)
    inventory = InventoryRepository(sessions)
    result = await collect(Settings(), apartments, inventory)
    assert result == {"fetched": 1, "stored": 1, "queued": 1}
    claimed = await inventory.claim_due()
    assert claimed is not None
    assert claimed.apartment.source_url == ad.source_url
    assert claimed.apartment.phone == ad.phone
    lalafo.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_collector_skips_published_cards_and_keeps_room_filter(repositories, monkeypatch):
    apartments, _, sessions = repositories
    old = make_ad(lalafo_id=-100, source_url="https://t.me/bishkekarendakv/100")
    stored = await apartments.upsert_discovered(old)
    await apartments.mark_published(stored.id, chat_id=-1001, message_id=100)
    two_rooms = make_ad(lalafo_id=-101, source_url="https://t.me/bishkekarendakv/101", rooms="2")
    monkeypatch.setattr("scripts.collect_telegram_inventory.fetch_telegram_apartments", AsyncMock(return_value=[old, two_rooms]))
    result = await collect(Settings(), apartments, InventoryRepository(sessions))
    assert result == {"fetched": 2, "stored": 0, "queued": 0}

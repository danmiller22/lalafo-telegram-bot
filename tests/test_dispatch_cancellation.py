import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import publish_inventory


@pytest.mark.asyncio
async def test_dispatch_cancellation_releases_claim_and_propagates(monkeypatch):
    inventory = SimpleNamespace(
        claim_due=AsyncMock(return_value=SimpleNamespace(id=17)),
        retry_item=AsyncMock(),
    )
    monkeypatch.setattr(publish_inventory, 'get_settings', lambda: SimpleNamespace())
    monkeypatch.setattr(publish_inventory, 'InventoryRepository', lambda _: inventory)
    monkeypatch.setattr(publish_inventory, 'ApartmentRepository', lambda _: object())
    monkeypatch.setattr(
        publish_inventory, '_publish_item', AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await publish_inventory.run(sessions=object())
    inventory.retry_item.assert_awaited_once_with(17, error='DispatchCancelled')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['published', 'skipped'])
async def test_late_cancellation_preserves_terminal_queue_item(monkeypatch, status):
    from app.inventory import InventoryRepository

    row = SimpleNamespace(status=status)
    session = AsyncMock()
    session.get.return_value = row
    context = AsyncMock()
    context.__aenter__.return_value = session
    sessions = SimpleNamespace(begin=lambda: context)
    await InventoryRepository(sessions).retry_item(17, error='DispatchCancelled')
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_dispatch_does_not_block_next_apartment(monkeypatch, repositories):
    from datetime import datetime, timedelta, timezone
    from app.inventory import InventoryRepository
    from app.models import ApartmentInventoryQueue
    from tests.helpers import make_ad

    apartments, _, sessions = repositories
    now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    for sequence in range(2):
        apartment = await apartments.upsert_discovered(make_ad(lalafo_id=990001 + sequence))
        async with sessions.begin() as session:
            session.add(ApartmentInventoryQueue(
                apartment_id=apartment.id, status='queued', sequence=sequence,
                scheduled_at=now - timedelta(minutes=1), window_key='cancel-test',
            ))
    inventory = InventoryRepository(sessions)
    first = await inventory.claim_due(now=now)
    assert first is not None
    await inventory.retry_item(first.id, error='DispatchCancelled')
    following = await inventory.claim_due(now=now)
    assert following is not None
    assert following.id != first.id

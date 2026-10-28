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
@pytest.mark.parametrize('status', ['published', 'skipped', 'uncertain'])
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
async def test_uncertain_telegram_send_survives_collection_and_deployment_queue_reset(repositories):
    from datetime import datetime, timezone
    from app.inventory import InventoryRepository
    from app.models import ApartmentInventoryQueue
    from tests.helpers import make_ad
    apartments, _, sessions = repositories
    ad=make_ad(lalafo_id=998877)
    apartment=await apartments.upsert_discovered(ad)
    now=datetime(2026,10,28,10,tzinfo=timezone.utc)
    async with sessions.begin() as session:
        row=ApartmentInventoryQueue(apartment_id=apartment.id,status='publishing',scheduled_at=now,window_key='ambiguous',sequence=1)
        session.add(row);await session.flush();qid=row.id
    inventory=InventoryRepository(sessions)
    await apartments.mark_publication_uncertain(apartment.id)
    await inventory.finish_item(qid,status='uncertain',error='TelegramPublishAmbiguousError')
    await inventory.retry_item(qid,error='DispatchCancelled')
    assert await inventory.claim_due(now=now) is None
    await apartments.upsert_discovered(ad)
    await inventory.reset_publication_history_for_code('uncertain-rebuild-test')
    assert await inventory.schedule_period(now=now)==0
    assert (await apartments.get(apartment.id)).publication_status=='uncertain'
    # Reconciliation of a real Telegram receipt restores the normal state.
    await apartments.mark_published(apartment.id,chat_id=-1001,message_id=4242,acknowledged_at=now)
    assert (await apartments.get(apartment.id)).publication_status=='published'


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


@pytest.mark.asyncio
async def test_timed_out_apartment_does_not_consume_the_dispatch_tick(monkeypatch):
    first = SimpleNamespace(id=17)
    second = SimpleNamespace(id=18)
    inventory = SimpleNamespace(
        claim_due=AsyncMock(side_effect=[first, second]),
        retry_item=AsyncMock(),
    )
    calls = 0

    async def publish(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(10)
        return 0

    real_timeout = asyncio.timeout
    monkeypatch.setattr(publish_inventory, 'get_settings', lambda: SimpleNamespace())
    monkeypatch.setattr(publish_inventory, 'InventoryRepository', lambda _: inventory)
    monkeypatch.setattr(publish_inventory, 'ApartmentRepository', lambda _: object())
    monkeypatch.setattr(publish_inventory, '_publish_item', publish)
    monkeypatch.setattr(
        publish_inventory.asyncio, 'timeout', lambda _seconds: real_timeout(0.001)
    )

    assert await publish_inventory.run(sessions=object()) == 0
    inventory.retry_item.assert_awaited_once_with(17, error='ItemPublishTimeout')
    assert calls == 2

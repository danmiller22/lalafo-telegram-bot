from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import publish_inventory_batch


@pytest.mark.asyncio
async def test_batch_keeps_claiming_until_six_cards_are_published(monkeypatch):
    counts = iter((1, 2, 3, 4, 5, 6))
    cycle = AsyncMock(return_value=0)
    publish = AsyncMock(return_value=0)
    sleep = AsyncMock()

    monkeypatch.setenv("PUBLISH_BATCH_SIZE", "6")
    monkeypatch.setenv("PUBLISH_BATCH_SPACING_SECONDS", "480")
    monkeypatch.setattr(publish_inventory_batch, "run_inventory_cycle", cycle)
    monkeypatch.setattr(publish_inventory_batch, "publish_one", publish)
    monkeypatch.setattr(
        publish_inventory_batch,
        "_has_due_queue",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        publish_inventory_batch,
        "_published_since",
        AsyncMock(side_effect=lambda _started_at: next(counts)),
    )
    monkeypatch.setattr(publish_inventory_batch.asyncio, "sleep", sleep)

    assert await publish_inventory_batch.run() == 0
    cycle.assert_awaited_once()
    assert publish.await_count == 5
    assert sleep.await_count == 5


@pytest.mark.asyncio
async def test_batch_does_not_consume_next_window_before_it_is_due(monkeypatch):
    cycle = AsyncMock(return_value=0)
    publish = AsyncMock(return_value=0)

    monkeypatch.delenv("FORCE_PUBLISH", raising=False)
    monkeypatch.setattr(publish_inventory_batch, "run_inventory_cycle", cycle)
    monkeypatch.setattr(publish_inventory_batch, "publish_one", publish)
    monkeypatch.setattr(
        publish_inventory_batch,
        "_has_due_queue",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        publish_inventory_batch,
        "_published_since",
        AsyncMock(return_value=0),
    )

    assert await publish_inventory_batch.run() == 0
    cycle.assert_awaited_once()
    publish.assert_not_awaited()

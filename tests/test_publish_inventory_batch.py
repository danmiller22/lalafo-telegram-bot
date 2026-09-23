from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import publish_inventory_batch


@pytest.mark.asyncio
async def test_cloud_tick_publishes_at_most_one_randomly_scheduled_card(monkeypatch):
    cycle = AsyncMock(return_value=0)
    monkeypatch.setattr(publish_inventory_batch, "run_inventory_cycle", cycle)

    assert await publish_inventory_batch.run() == 0

    cycle.assert_awaited_once_with()

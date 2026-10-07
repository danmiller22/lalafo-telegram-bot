from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.inventory import InventoryRepository
from scripts import collect_lalafo_inventory as collector
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_primary_collector_respects_running_github_lease(repositories, monkeypatch):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    assert await inventory.claim_discovery(now=datetime.now(timezone.utc)) is not None
    discover = AsyncMock()
    select = AsyncMock()
    monkeypatch.setattr(collector, "discover", discover)
    monkeypatch.setattr(collector, "find_working_proxies", select)
    assert await collector.collect(Settings(), inventory) == {"status": "not_due", "exit_code": 0}
    discover.assert_not_awaited()
    select.assert_not_awaited()


@pytest.mark.asyncio
async def test_primary_collector_refills_without_publishing_or_waiting_for_github(repositories, monkeypatch):
    apartments, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    settings = Settings()

    async def discover(**kwargs):
        assert kwargs["discovery_only"]
        assert not kwargs["include_telegram_sources"]
        assert settings.lalafo_proxy_url == "http://route-1,http://route-2"
        await apartments.upsert_discovered(make_ad())
        await inventory.schedule_period()
        kwargs["discovery_stats"].update(stored=1, search_pages=1, search_results=20)
        return 0

    monkeypatch.setattr(collector, "discover", discover)
    monkeypatch.setattr(collector, "find_working_proxies", AsyncMock(return_value=["http://route-1", "http://route-2"]))
    result = await collector.collect(settings, inventory)
    assert result["stored"] == 1
    assert result["verified_routes"] == 2
    assert result["period_queue"] == 1
    claimed = await inventory.claim_due()
    assert claimed is not None
    assert claimed.apartment.phone == make_ad().phone


@pytest.mark.asyncio
async def test_primary_collector_failure_releases_lease_and_keeps_configured_route(repositories, monkeypatch):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    settings = Settings(lalafo_proxy_url="http://configured")
    monkeypatch.setattr(collector, "find_working_proxies", AsyncMock(side_effect=RuntimeError("unavailable")))
    monkeypatch.setattr(collector, "discover", AsyncMock(side_effect=RuntimeError("temporary")))
    result = await collector.collect(settings, inventory)
    assert settings.lalafo_proxy_url == "http://configured"
    assert result["status"] == "retryable"
    assert result["error"] == "RuntimeError"
    # Forced operator retry can reclaim the finished run immediately.
    assert await inventory.claim_discovery(now=datetime.now(timezone.utc), force=True) is not None

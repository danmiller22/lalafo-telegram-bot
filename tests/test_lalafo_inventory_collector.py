from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.inventory import InventoryRepository
from scripts import collect_lalafo_inventory as collector
from tests.helpers import make_ad

pytestmark = pytest.mark.usefixtures("publication_clock")


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
        assert kwargs["candidate_pool_limit_override"] == 96
        assert settings.apartment_detail_concurrency <= 2
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


@pytest.mark.asyncio
async def test_primary_collector_preserves_exact_http_access_failure(repositories, monkeypatch):
    _, _, sessions = repositories
    inventory = InventoryRepository(sessions)
    selector = AsyncMock(return_value=[])
    async def blocked(**kwargs):
        kwargs["discovery_stats"]["last_source_error"] = {
            "type": "LalafoAccessError", "http_status": 403,
        }
        return 2
    monkeypatch.setattr(collector, "discover", blocked)
    monkeypatch.setattr(collector, "find_working_proxies", selector)
    result = await collector.collect(Settings(lalafo_proxy_url="http://configured"), inventory)
    selector.assert_awaited_once_with("http://configured")
    assert result["status"] == "retryable"
    assert result["error"] == "LalafoAccessError:HTTP403"
    assert result["search_pages"] == 0


@pytest.mark.asyncio
async def test_primary_collector_never_reports_success_without_reading_a_source_page(repositories, monkeypatch):
    _, _, sessions = repositories
    monkeypatch.setattr(collector, "discover", AsyncMock(return_value=0))
    monkeypatch.setattr(collector, "find_working_proxies", AsyncMock(return_value=[]))
    result = await collector.collect(Settings(), InventoryRepository(sessions))
    assert result["status"] == "retryable"
    assert result["exit_code"] == 2
    assert result["error"] == "NoSourcePagesRead"


@pytest.mark.asyncio
async def test_timeout_retains_saved_inventory_and_partial_collection_metrics(repositories, monkeypatch):
    apartments, _, sessions = repositories
    inventory = InventoryRepository(sessions)

    async def interrupted(**kwargs):
        await apartments.upsert_discovered(make_ad())
        kwargs["discovery_stats"].update(stored=1, search_pages=1, search_results=20)
        raise TimeoutError()

    monkeypatch.setattr(collector, "discover", interrupted)
    monkeypatch.setattr(collector, "find_working_proxies", AsyncMock(return_value=[]))
    result = await collector.collect(Settings(), inventory)
    assert result["status"] == "retryable"
    assert result["error"] == "TimeoutError"
    assert result["stored"] == 1
    assert result["search_pages"] == 1
    assert await apartments.get_by_lalafo(make_ad().lalafo_id) is not None

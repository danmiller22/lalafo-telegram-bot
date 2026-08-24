from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import get_settings
from app import web


@pytest.fixture(autouse=True)
def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_TRIGGER_SECRET", "x" * 32)
    get_settings.cache_clear()
    web._run_state.update(
        running=False,
        last_started_at=None,
        last_finished_at=None,
        last_exit_code=None,
    )
    web._scraper_task = None
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_and_authentication() -> None:
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.post("/run")).status_code == 401
        response = await client.get(
            "/status", headers={"Authorization": f"Bearer {'x' * 32}"}
        )
    assert response.status_code == 200
    assert response.json()["running"] is False


@pytest.mark.asyncio
async def test_trigger_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_run() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return 0

    import scripts.scrape_publish

    monkeypatch.setattr(scripts.scrape_publish, "run", fake_run)
    transport = httpx.ASGITransport(app=web.app)
    headers = {"Authorization": f"Bearer {'x' * 32}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/run", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "completed", "exit_code": 0}
        status_response = await client.get("/status", headers=headers)
    assert calls == 1
    assert status_response.json()["last_exit_code"] == 0

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import get_settings
from app.security import TokenSigner
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
    web._bot_runtime = None
    web._keyboard_sync_task = None
    web._lalafo_auto_responder = None
    web._lalafo_watchdog_task = None
    web._apartment_scheduler_task = None
    web._apartment_scheduler_state.update(
        running_cycle=False,
        last_check_at=None,
        last_exit_code=None,
        last_error=None,
        recent_published_count=None,
        latest_published_at=None,
        schedule_status=None,
        schedule_due=None,
        schedule_last_started_at=None,
        schedule_last_completed_at=None,
    )
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_and_authentication() -> None:
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "bot": "disabled",
            "lalafo_auto_reply": "disabled",
            "apartment_scheduler": "disabled",
        }
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


@pytest.mark.asyncio
async def test_health_fails_when_enabled_bot_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "bot": "stopped"}


@pytest.mark.asyncio
async def test_health_fails_when_auto_reply_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LALAFO_AUTO_REPLY_ENABLED", "true")
    get_settings.cache_clear()
    web._lalafo_auto_responder = SimpleNamespace(
        status=lambda **_: {"state": "recovering"},
        is_healthy=lambda **_: False,
    )
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["lalafo_auto_reply"] == {"state": "recovering"}


@pytest.mark.asyncio
async def test_restart_replaces_only_unhealthy_auto_reply_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = SimpleNamespace(
        is_healthy=lambda **_: False,
        close=AsyncMock(),
    )
    started = False

    def start() -> None:
        nonlocal started
        started = True

    replacement = SimpleNamespace(start=start)
    web._lalafo_auto_responder = old
    monkeypatch.setattr(web, "_build_lalafo_auto_responder", lambda: replacement)

    await web._restart_lalafo_auto_responder("test")

    old.close.assert_awaited_once()
    assert web._lalafo_auto_responder is replacement
    assert started


@pytest.mark.asyncio
async def test_hosted_scheduler_runs_due_check_without_forcing_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.publish_if_due

    select_proxies = AsyncMock()
    run_if_due = AsyncMock(return_value=0)
    publication_status = AsyncMock(side_effect=[(0, None), (5, None)])
    schedule_status = AsyncMock(
        side_effect=[
            SimpleNamespace(
                status="idle",
                due=True,
                lease_active=False,
                last_started_at=None,
                last_completed_at=None,
            ),
            SimpleNamespace(
                status="succeeded",
                due=False,
                lease_active=False,
                last_started_at=None,
                last_completed_at=None,
            ),
        ]
    )
    monkeypatch.setattr(web, "_select_hosted_lalafo_proxies", select_proxies)
    monkeypatch.setattr(scripts.publish_if_due, "run", run_if_due)
    monkeypatch.setattr(
        scripts.publish_if_due,
        "publication_window_status",
        publication_status,
    )
    monkeypatch.setattr(
        scripts.publish_if_due,
        "publication_schedule_status",
        schedule_status,
    )

    assert await web._execute_due_apartment_cycle() == 0

    select_proxies.assert_awaited_once()
    run_if_due.assert_awaited_once_with(
        force=False,
        window_minutes=120,
        max_attempts=3,
    )
    assert publication_status.await_count == 2
    assert schedule_status.await_count == 2
    assert web._apartment_scheduler_state["recent_published_count"] == 5
    assert web._apartment_scheduler_state["last_exit_code"] == 0
    assert web._apartment_scheduler_state["running_cycle"] is False


@pytest.mark.asyncio
async def test_telegram_webhook_requires_secret_and_dispatches_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    webhook_secret = "w" * 32
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", webhook_secret)
    get_settings.cache_clear()
    feed_update = AsyncMock()
    web._bot_runtime = SimpleNamespace(
        bot=object(),
        dispatcher=SimpleNamespace(feed_update=feed_update),
        workflow_data={"marker": "test"},
    )
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/telegram/webhook", json={"update_id": 1})
        accepted = await client.post(
            "/telegram/webhook",
            json={"update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        )
    assert denied.status_code == 401
    assert accepted.status_code == 200
    feed_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_payment_redirect_replaces_button_and_opens_finik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("CALLBACK_SECRET", "c" * 32)
    monkeypatch.setenv("FINIK_PAYMENT_URL", "https://qr.finik.kg/test-payment")
    get_settings.cache_clear()
    edit_markup = AsyncMock()
    web._bot_runtime = SimpleNamespace(
        bot=SimpleNamespace(edit_message_reply_markup=edit_markup)
    )
    signer = TokenSigner("c" * 32)
    token = signer.sign_values("finik-redirect", 11, 22, 33)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/pay/{token}")
        invalid = await client.get(f"/pay/{token}x")
    assert response.status_code == 302
    assert response.headers["location"] == "https://qr.finik.kg/test-payment"
    assert invalid.status_code == 404
    edit_markup.assert_awaited_once()

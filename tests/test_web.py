from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest

from app.config import get_settings
from app.payment_plans import LIFETIME_PLAN, MONTH_PLAN, WEEK_PLAN
from app.security import TokenSigner
from app import web


def miniapp_init_data(*, bot_token: str, user_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE-web-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test", "username": "mini_user"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_approved_miniapp_payload_contains_apartment_card() -> None:
    apartment = SimpleNamespace(
        rooms="2",
        district="ЦУМ",
        city="Бишкек",
        price=35_000,
        deposit=5_000,
        photo_urls=[
            "https://img.example/1.jpg",
            "https://img.example/2.jpg",
            "https://img.example/3.jpg",
            "https://img.example/4.jpg",
            "https://img.example/5.jpg",
        ],
        phone="+996555123456",
    )
    result = SimpleNamespace(
        status="approved",
        apartment=apartment,
        access_expires_at=None,
    )

    payload = web._miniapp_result_payload(result)

    assert payload["phone"] == "+996 555 123 456"
    assert payload["apartment"] == {
        "rooms": "2",
        "district": "ЦУМ",
        "city": "Бишкек",
        "price": 35_000,
            "deposit": 5_000,
            "description": "",
        "photo_urls": [
            "https://img.example/1.jpg",
            "https://img.example/2.jpg",
            "https://img.example/3.jpg",
            "https://img.example/4.jpg",
        ],
    }


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
    web._bot_setup_task = None
    web._lalafo_bot_setup_task = None
    web._legacy_featured_cleanup_task = None
    web._keyboard_sync_task = None
    web._lalafo_auto_responder = None
    web._lalafo_watchdog_task = None
    web._apartment_scheduler_task = None
    web._service_keepalive_task = None
    web._background_watchdog_task = None
    web._shutting_down = False
    web._bot_setup_state.update(
        state="pending",
        last_configured_at=None,
        last_error=None,
    )
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
    web._service_keepalive_state.update(
        state="pending",
        last_success_at=None,
        last_error=None,
        consecutive_failures=0,
    )
    web._background_watchdog_state.update(
        state="pending",
        last_check_at=None,
        last_error=None,
        restart_count=0,
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
            "finik_auto_payment": "disabled",
        "payment_access_mode": "manual",
        "listing_validity_days": 2,
        "payment_receipt_required": False,
        "contact_tariff": {"plan": "week", "price": 500, "expires": True, "storage": "persistent_ledger"},
        "payment_review": "admin_missing",
            "telegram_setup": "disabled",
            "lalafo_link_bot": "disabled",
            "free_cloud_keepalive": "disabled",
            "background_watchdog": {
                "state": "pending",
                "last_check_at": None,
                "last_error": None,
                "restart_count": 0,
            },
            "personal_matching": "disabled",
            "lalafo_auto_reply": "disabled",
            "apartment_scheduler": "disabled",
        }
        assert (await client.post("/run")).status_code == 401
        response = await client.get(
            "/status", headers={"Authorization": f"Bearer {'x' * 32}"}
        )
    assert response.status_code == 200
    assert response.json()["running"] is False


def test_apartment_interval_cannot_be_overridden_by_stale_cloud_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTED_APARTMENT_PUBLISH_INTERVAL_MINUTES", "120")
    get_settings.cache_clear()

    assert get_settings().hosted_apartment_publish_interval_minutes == 90


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
async def test_health_keeps_main_service_live_when_auto_reply_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LALAFO_AUTO_REPLY_ENABLED", "true")
    monkeypatch.setenv("LALAFO_AUTO_REPLY_WEB_ENABLED", "true")
    get_settings.cache_clear()
    web._lalafo_auto_responder = SimpleNamespace(
        status=lambda **_: {"state": "recovering"},
        is_healthy=lambda **_: False,
    )
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["lalafo_auto_reply"] == {"state": "recovering"}


@pytest.mark.asyncio
async def test_health_keeps_payment_bot_live_when_scheduler_is_recovering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    get_settings.cache_clear()
    web._bot_runtime = object()  # type: ignore[assignment]
    web._apartment_scheduler_task = None
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["bot"] == "running"
    assert response.json()["apartment_scheduler"]["state"] == "recovering"


@pytest.mark.asyncio
async def test_telegram_network_setup_has_a_bounded_one_shot_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s" * 32)
    get_settings.cache_clear()
    bot = SimpleNamespace(set_webhook=AsyncMock())
    runtime = SimpleNamespace(
        bot=bot,
        dispatcher=SimpleNamespace(resolve_used_update_types=lambda: ["message"]),
    )
    configure_profile = AsyncMock()
    web._bot_runtime = runtime  # type: ignore[assignment]
    monkeypatch.setattr(web, "configure_bot_profile", configure_profile)

    await web._configure_main_bot_once()

    bot.set_webhook.assert_awaited_once()
    configure_profile.assert_awaited_once_with(runtime)


@pytest.mark.asyncio
async def test_free_cloud_keepalive_uses_public_health_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL", "https://service.example/telegram/webhook"
    )
    get_settings.cache_clear()
    requested = asyncio.Event()
    urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, url: str):
            urls.append(url)
            requested.set()
            return FakeResponse()

    monkeypatch.setattr(web.httpx, "AsyncClient", FakeClient)
    task = asyncio.create_task(web._keep_service_awake())
    await asyncio.wait_for(requested.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert urls == ["https://service.example/health"]
    assert web._service_keepalive_state["state"] == "running"
    assert web._service_keepalive_state["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_background_watchdog_restarts_only_stopped_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("SERVICE_KEEPALIVE_ENABLED", "false")
    monkeypatch.setattr(web, "IN_PROCESS_QUEUE_DISPATCHER_ENABLED", True)
    get_settings.cache_clear()
    stop = asyncio.Event()

    async def running_worker() -> None:
        await stop.wait()

    async def stopped_worker() -> None:
        return None

    bot_setup = asyncio.create_task(running_worker())
    stopped_scheduler = asyncio.create_task(stopped_worker())
    await stopped_scheduler
    web._bot_setup_task = bot_setup
    web._apartment_scheduler_task = stopped_scheduler
    monkeypatch.setattr(web, "_run_hosted_apartment_scheduler", running_worker)

    assert await web._repair_background_tasks_once() == 1
    replacement = web._apartment_scheduler_task
    assert replacement is not None and not replacement.done()
    assert web._bot_setup_task is bot_setup

    stop.set()
    await asyncio.gather(bot_setup, replacement)


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
    select_proxies = AsyncMock()
    run_worker = AsyncMock(return_value=0)
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
    monkeypatch.setattr(web, "_run_inventory_worker_process", run_worker)
    import scripts.publish_if_due
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
    run_worker.assert_awaited_once_with(get_settings())
    assert publication_status.await_count == 2
    assert schedule_status.await_count == 2
    assert web._apartment_scheduler_state["recent_published_count"] == 5
    assert web._apartment_scheduler_state["last_exit_code"] == 0
    assert web._apartment_scheduler_state["running_cycle"] is False


@pytest.mark.asyncio
async def test_hosted_queue_dispatcher_publishes_without_proxy_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.publish_if_due
    import scripts.publish_inventory

    publish_one = AsyncMock(return_value=0)
    earlier = datetime(2026, 9, 23, 8, tzinfo=UTC)
    published = datetime(2026, 9, 23, 9, tzinfo=UTC)
    window_status = AsyncMock(side_effect=[(6, earlier), (7, published)])
    monkeypatch.setattr(scripts.publish_inventory, "run", publish_one)
    monkeypatch.setattr(
        web,
        "_inventory_queue_status",
        AsyncMock(return_value=(12, 2, published)),
    )
    monkeypatch.setattr(
        scripts.publish_if_due, "publication_window_status", window_status
    )

    assert await web._execute_queue_dispatch() == 0

    publish_one.assert_awaited_once()
    assert publish_one.await_args.kwargs["eligible_until"] > datetime.now(UTC)
    assert window_status.await_count == 2
    assert web._apartment_scheduler_state["recent_published_count"] == 7
    assert web._apartment_scheduler_state["queued_count"] == 12
    assert web._apartment_scheduler_state["due_count"] == 2
    assert web._apartment_scheduler_state["running_cycle"] is False


@pytest.mark.asyncio
async def test_hosted_scheduler_refills_an_empty_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refill = AsyncMock(return_value=0)
    dispatch = AsyncMock(return_value=0)
    monkeypatch.setattr(
        web,
        "_inventory_queue_status",
        AsyncMock(return_value=(0, 0, None)),
    )
    monkeypatch.setattr(web, "_refill_saved_inventory", refill)
    monkeypatch.setattr(web, "_execute_queue_dispatch", dispatch)
    monkeypatch.setattr(
        web.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await web._run_hosted_apartment_scheduler()

    refill.assert_awaited_once()
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedicated_lalafo_webhook_keeps_pending_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LALAFO_BOT_TOKEN", "123456:lalafo-test-token")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://service.example/telegram/webhook",
    )
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "w" * 32)
    get_settings.cache_clear()
    bot = SimpleNamespace(set_webhook=AsyncMock())
    dispatcher = SimpleNamespace(resolve_used_update_types=lambda: ["message"])
    web._lalafo_bot_runtime = SimpleNamespace(bot=bot, dispatcher=dispatcher)

    await web._configure_lalafo_bot_once()

    bot.set_webhook.assert_awaited_once_with(
        "https://service.example/telegram/lalafo-webhook",
        secret_token="w" * 32,
        allowed_updates=["message"],
        drop_pending_updates=False,
    )


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
async def test_retired_featured_webhook_is_gone() -> None:
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/featured/webhook", json={"update_id": 10})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_miniapp_page_is_public_but_session_requires_telegram_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_token = "123456:telegram-test-token"
    callback_secret = "c" * 32
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("CALLBACK_SECRET", callback_secret)
    get_settings.cache_clear()
    apartment = SimpleNamespace(
        rooms="1",
        district="ЦУМ",
        city="Бишкек",
        price=25_000,
        deposit=None,
        photo_urls=["https://img.example/apartment.jpg"],
        phone="+996555123456",
    )
    result = SimpleNamespace(
        status="unpaid",
        apartment=apartment,
        access_expires_at=None,
    )
    service = SimpleNamespace(contact_status=AsyncMock(return_value=result))
    monkeypatch.setattr(
        web,
        "_bot_runtime",
        SimpleNamespace(workflow_data={"service": service}),
    )
    signer = TokenSigner(callback_secret)
    start_param = signer.sign_start_id("miniapp-apartment", 42)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/miniapp")
        denied = await client.post(
            "/miniapp/api/session",
            json={"init_data": "invalid", "start_param": start_param},
        )
        accepted = await client.post(
            "/miniapp/api/session",
            json={
                "init_data": miniapp_init_data(bot_token=bot_token, user_id=778899),
                "start_param": start_param,
            },
        )

    assert page.status_code == 200
    assert "Telegram.WebApp" in page.text
    assert denied.status_code == 401
    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["status"] == "unpaid"
    assert payload["price"] == 500
    assert payload["monthly_available"] is False
    assert "terms_accepted" not in payload
    assert "terms_text" not in payload
    assert payload["privacy_url"].endswith("?start=privacy")
    assert "support_url" not in payload
    service.contact_status.assert_awaited_once_with(778899, 42)


def test_automatic_checkout_is_enabled_when_finik_api_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEKLY_FINIK_PAYMENT_URL", "https://qr.finik.kg/weekly")
    monkeypatch.setenv("MONTHLY_FINIK_PAYMENT_URL", "https://qr.finik.kg/monthly")
    monkeypatch.setenv("FINIK_API_KEY", "configured")
    monkeypatch.setenv("FINIK_ACCOUNT_ID", "corporate")
    monkeypatch.setenv("FINIK_PRIVATE_KEY_PEM", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.finik_auto_enabled
    assert web._uses_dynamic_finik(settings, WEEK_PLAN)
    assert web._uses_dynamic_finik(settings, MONTH_PLAN)
    assert web._finik_payment_url(settings, WEEK_PLAN).endswith("/weekly")
    assert web._finik_payment_url(settings, MONTH_PLAN).endswith("/monthly")


@pytest.mark.asyncio
async def test_miniapp_starts_a_checkout_bound_to_the_selected_apartment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEKLY_FINIK_PAYMENT_URL", "")
    bot_token = "123456:telegram-test-token"
    callback_secret = "c" * 32
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("CALLBACK_SECRET", callback_secret)
    monkeypatch.setenv("FINIK_API_KEY", "configured")
    monkeypatch.setenv("FINIK_ACCOUNT_ID", "corporate")
    monkeypatch.setenv("FINIK_PRIVATE_KEY_PEM", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    apartment = SimpleNamespace(id=42)
    unpaid = SimpleNamespace(status="unpaid", apartment=apartment, access_expires_at=None)
    payment_request = SimpleNamespace(id=73, status="awaiting_receipt")
    service = SimpleNamespace(
        contact_status=AsyncMock(return_value=unpaid),
        begin_payment=AsyncMock(
            return_value=SimpleNamespace(request=payment_request, outcome="created")
        ),
    )
    payments = SimpleNamespace()
    monkeypatch.setattr(
        web,
        "_bot_runtime",
        SimpleNamespace(workflow_data={"service": service, "payments": payments, "terms_consents": SimpleNamespace(accept=AsyncMock())}),
    )
    checkout = AsyncMock(return_value="https://qr.finik.kg/request-73")
    monkeypatch.setattr(web, "_finik_checkout_url", checkout)
    signer = TokenSigner(callback_secret)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/miniapp/api/start",
            json={
                "init_data": miniapp_init_data(bot_token=bot_token, user_id=778899),
                "start_param": signer.sign_start_id("miniapp-apartment", 42),
                "plan": MONTH_PLAN,
            },
        )

    assert response.status_code == 200
    assert response.json()["payment_url"] == "https://qr.finik.kg/request-73"
    assert response.json()["automatic_payment"] is True
    assert response.json()["monthly_available"] is False
    service.begin_payment.assert_awaited_once_with(
        user_id=778899,
        apartment_id=42,
        username="mini_user",
        first_name="Test",
        plan=WEEK_PLAN,
    )
    checkout.assert_awaited_once()
    assert checkout.await_args.kwargs["apartment_id"] == 42
    assert checkout.await_args.kwargs["plan"] == WEEK_PLAN


@pytest.mark.asyncio
async def test_miniapp_uses_configured_payment_url_without_waiting_for_finik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_token = "123456:telegram-test-token"
    callback_secret = "c" * 32
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("CALLBACK_SECRET", callback_secret)
    monkeypatch.setenv("WEEKLY_FINIK_PAYMENT_URL", "https://qr.finik.kg/weekly")
    monkeypatch.setenv("FINIK_API_KEY", "configured")
    monkeypatch.setenv("FINIK_ACCOUNT_ID", "corporate")
    monkeypatch.setenv("FINIK_PRIVATE_KEY_PEM", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    apartment = SimpleNamespace(id=42)
    unpaid = SimpleNamespace(status="unpaid", apartment=apartment, access_expires_at=None)
    payment_request = SimpleNamespace(id=73, status="awaiting_receipt")
    service = SimpleNamespace(
        contact_status=AsyncMock(return_value=unpaid),
        begin_payment=AsyncMock(
            return_value=SimpleNamespace(request=payment_request, outcome="created")
        ),
    )
    monkeypatch.setattr(
        web,
        "_bot_runtime",
        SimpleNamespace(workflow_data={"service": service, "payments": SimpleNamespace(), "terms_consents": SimpleNamespace(accept=AsyncMock())}),
    )
    checkout = AsyncMock(return_value="https://qr.finik.kg/request-73")
    monkeypatch.setattr(web, "_finik_checkout_url", checkout)
    signer = TokenSigner(callback_secret)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/miniapp/api/start",
            json={
                "init_data": miniapp_init_data(bot_token=bot_token, user_id=778899),
                "start_param": signer.sign_start_id("miniapp-apartment", 42),
                "plan": MONTH_PLAN,
            },
        )

    assert response.status_code == 200
    assert response.json()["payment_url"] == "https://qr.finik.kg/weekly"
    assert response.json()["automatic_payment"] is True
    service.begin_payment.assert_awaited_once()
    checkout.assert_not_awaited()


@pytest.mark.asyncio
async def test_miniapp_access_sends_customer_claim_to_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_token = "123456:telegram-test-token"
    callback_secret = "c" * 32
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("CALLBACK_SECRET", callback_secret)
    monkeypatch.setenv("ADMIN_USER_ID", "999")
    monkeypatch.setenv("FINIK_API_KEY", "configured")
    monkeypatch.setenv("FINIK_ACCOUNT_ID", "corporate")
    monkeypatch.setenv("FINIK_PRIVATE_KEY_PEM", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    apartment = SimpleNamespace(
        id=42,
        rooms="1",
        district="ЦУМ",
        city="Бишкек",
        price=25_000,
        deposit=None,
        photo_urls=["https://img.example/apartment.jpg"],
        phone="+996555123456",
    )
    awaiting = SimpleNamespace(
        status="awaiting_receipt", apartment=apartment, access_expires_at=None
    )
    approved = SimpleNamespace(
        status="pending", apartment=apartment, access_expires_at=None
    )
    request = SimpleNamespace(
        id=73,
        telegram_user_id=778899,
        username="mini_user",
        first_name="Test",
        plan="week",
        status="pending",
        apartment=apartment,
        receipt_file_id=None,
    )
    service = SimpleNamespace(
        contact_status=AsyncMock(side_effect=[awaiting, approved]),
    )
    payments = SimpleNamespace(
        mark_payment_claimed=AsyncMock(return_value=request),
        get_access=AsyncMock(),
        claim_admin_notification=AsyncMock(return_value=True),
        finish_admin_notification=AsyncMock(return_value=True),
        release_admin_notification=AsyncMock(),
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=515)),
    )
    monkeypatch.setattr(
        web,
        "_bot_runtime",
        SimpleNamespace(
            bot=bot,
            workflow_data={"service": service, "payments": payments},
        ),
    )
    signer = TokenSigner(callback_secret)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/miniapp/api/access",
            json={
                "init_data": miniapp_init_data(bot_token=bot_token, user_id=778899),
                "start_param": signer.sign_start_id("miniapp-apartment", 42),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    payments.mark_payment_claimed.assert_awaited_once_with(user_id=778899, apartment_id=42)
    payments.claim_admin_notification.assert_awaited_once_with(73)
    payments.finish_admin_notification.assert_awaited_once_with(73, 515)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_finik_webhook_does_not_deliver_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("FINIK_API_KEY", "configured")
    monkeypatch.setenv("FINIK_ACCOUNT_ID", "corporate")
    monkeypatch.setenv("FINIK_PRIVATE_KEY_PEM", "configured")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    apartment = SimpleNamespace(id=42)
    payment_request = SimpleNamespace(
        id=73,
        telegram_user_id=778899,
        apartment=apartment,
    )
    payments = SimpleNamespace(
        apply_provider_result=AsyncMock(return_value=("awaiting_confirmation", payment_request))
    )
    bot = object()
    monkeypatch.setattr(
        web,
        "_bot_runtime",
        SimpleNamespace(bot=bot, workflow_data={"payments": payments}),
    )
    monkeypatch.setattr(web, "verify_request", lambda *_args, **_kwargs: True)
    delivery = AsyncMock()
    monkeypatch.setattr("app.telegram.private_delivery.send_private_contact", delivery)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/finik/webhook",
            headers={
                "signature": "valid-signature",
                "x-api-timestamp": str(int(time.time() * 1000)),
            },
            json={
                "status": "success",
                "amount": 499,
                "fields": {"paymentId": "arenda-73"},
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "awaiting_confirmation"}
    payments.apply_provider_result.assert_awaited_once_with(
        "arenda-73", succeeded=True, amount=499
    )
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_payment_redirect_opens_current_miniapp_tariff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_BOT", "true")
    monkeypatch.setenv("CALLBACK_SECRET", "c" * 32)
    monkeypatch.setenv("WEEKLY_FINIK_PAYMENT_URL", "https://qr.finik.kg/test-payment")
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
    assert "/access?startapp=" in response.headers["location"]
    assert signer.verify_start_id("miniapp-apartment", response.headers["location"].split("startapp=", 1)[1]) == 11
    assert invalid.status_code == 404
    edit_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_lifetime_checkout_uses_ready_link_even_with_api_credentials():
    from app.config import Settings
    settings = Settings(_env_file=None, lifetime_finik_payment_url="https://qr.finik.kg/e0c9972e-0f05-4dc3-99fd-96ec1debea1f?type=t", finik_api_key="configured", finik_account_id="configured", finik_private_key_pem="configured")
    assert not web._uses_dynamic_finik(settings, LIFETIME_PLAN)
    assert await web._finik_checkout_url(settings, object(), object(), apartment_id=42, plan=LIFETIME_PLAN) == settings.lifetime_finik_payment_url

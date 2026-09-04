from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile, Update
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.bot.main import BotRuntime, configure_bot_profile, create_runtime
from app.config import get_settings
from app.lalafo.auto_reply import LalafoAutoResponder
from app.payment_plans import WEEK_PLAN, WEEK_PRICE
from app.security import TokenSigner
from app.telegram.formatting import format_admin_card, format_apartment, room_title
from app.telegram.keyboards import admin_keyboard, paid_keyboard
from app.telegram.miniapp import mini_app_html, verify_telegram_init_data
from app.lalafo.phone import display_phone

logger = logging.getLogger(__name__)
app = FastAPI(title="Lalafo Telegram service", docs_url=None, redoc_url=None)

_run_lock = asyncio.Lock()
_scraper_task: asyncio.Task[None] | None = None
_bot_runtime: BotRuntime | None = None
_bot_setup_task: asyncio.Task[None] | None = None
_legacy_featured_cleanup_task: asyncio.Task[None] | None = None
_keyboard_sync_task: asyncio.Task[None] | None = None
_lalafo_auto_responder: LalafoAutoResponder | None = None
_lalafo_watchdog_task: asyncio.Task[None] | None = None
_lalafo_restart_lock = asyncio.Lock()
_apartment_scheduler_task: asyncio.Task[None] | None = None
_service_keepalive_task: asyncio.Task[None] | None = None
_background_watchdog_task: asyncio.Task[None] | None = None
_shutting_down = False
_bot_setup_state: dict[str, Any] = {
    "state": "pending",
    "last_configured_at": None,
    "last_error": None,
}
_run_state: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
}
_apartment_scheduler_state: dict[str, Any] = {
    "running_cycle": False,
    "last_check_at": None,
    "last_exit_code": None,
    "last_error": None,
    "recent_published_count": None,
    "latest_published_at": None,
    "schedule_status": None,
    "schedule_due": None,
    "schedule_last_started_at": None,
    "schedule_last_completed_at": None,
}
_service_keepalive_state: dict[str, Any] = {
    "state": "pending",
    "last_success_at": None,
    "last_error": None,
    "consecutive_failures": 0,
}
_background_watchdog_state: dict[str, Any] = {
    "state": "pending",
    "last_check_at": None,
    "last_error": None,
    "restart_count": 0,
}


class MiniAppRequest(BaseModel):
    init_data: str
    start_param: str


class MiniAppReceiptRequest(MiniAppRequest):
    file_name: str
    content_type: str
    file_base64: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _authorize(authorization: str | None) -> None:
    expected = get_settings().require_run_trigger_secret()
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _configure_main_bot_once() -> None:
    """Apply the webhook/profile with a deadline outside startup."""
    settings = get_settings()
    runtime = _bot_runtime
    if runtime is None:
        return
    async with asyncio.timeout(30.0):
        await runtime.bot.set_webhook(
            settings.require_telegram_webhook_url(),
            secret_token=settings.require_telegram_webhook_secret(),
            allowed_updates=runtime.dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        await configure_bot_profile(runtime)


async def _configure_main_bot() -> None:
    """Continuously repair Telegram configuration without blocking customers."""
    backoff = 2.0
    while True:
        try:
            if _bot_runtime is None:
                return
            await _configure_main_bot_once()
            _bot_setup_state.update(
                state="ready",
                last_configured_at=_now(),
                last_error=None,
            )
            logger.info("Telegram webhook and commands are ready")
            backoff = 2.0
            # Re-assert the webhook periodically in case an external tool or an
            # old deployment overwrites it. This call is idempotent.
            await asyncio.sleep(6 * 60 * 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _bot_setup_state.update(
                state="recovering",
                last_error=type(exc).__name__,
            )
            logger.exception("Telegram background setup failed; retrying")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _keep_service_awake() -> None:
    """Generate minimal inbound traffic so Koyeb Free does not cold-sleep."""
    settings = get_settings()
    health_url = settings.require_public_base_url() + "/health"
    interval = max(300.0, settings.service_keepalive_seconds)
    retry_interval = min(60.0, interval)
    timeout = max(3.0, settings.service_keepalive_timeout_seconds)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={"User-Agent": "lalafo-payment-bot-keepalive/1"},
    ) as client:
        while True:
            delay = interval
            try:
                response = await client.get(health_url)
                response.raise_for_status()
                _service_keepalive_state.update(
                    state="running",
                    last_success_at=_now(),
                    last_error=None,
                    consecutive_failures=0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures = int(
                    _service_keepalive_state.get("consecutive_failures") or 0
                ) + 1
                _service_keepalive_state.update(
                    state="recovering",
                    last_error=type(exc).__name__,
                    consecutive_failures=failures,
                )
                logger.warning("Free-cloud keepalive failed: %s", type(exc).__name__)
                delay = retry_interval
            await asyncio.sleep(delay)


async def _remove_legacy_featured_webhook() -> None:
    """Disconnect the retired advertising bot without delaying the main bot."""
    token = os.environ.get("FEATURED_REVIEW_BOT_TOKEN", "").strip()
    if not token:
        return
    bot = Bot(token=token)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Retired Lalafo advertising bot webhook removed")
    except Exception:
        logger.exception("Could not remove retired advertising bot webhook")
    finally:
        await bot.session.close()


async def _execute_scraper() -> int:
    async with _run_lock:
        _run_state.update(
            running=True,
            last_started_at=_now(),
            last_finished_at=None,
            last_exit_code=None,
        )
        try:
            from scripts.scrape_publish import run as run_scraper

            _run_state["last_exit_code"] = await run_scraper()
        except Exception:
            _run_state["last_exit_code"] = 1
            logger.exception("Hosted scraper run failed")
        finally:
            _run_state.update(running=False, last_finished_at=_now())
        return int(_run_state["last_exit_code"])


async def _sync_outdated_keyboards() -> None:
    try:
        from scripts.sync_published_keyboards import run as run_keyboard_sync

        exit_code = await run_keyboard_sync()
        if exit_code:
            logger.error("Hosted apartment keyboard sync exited with code %s", exit_code)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Hosted apartment keyboard sync failed")


def _build_lalafo_auto_responder() -> LalafoAutoResponder:
    settings = get_settings()
    login, password = settings.require_lalafo_auto_reply_credentials()
    return LalafoAutoResponder(
        login=login,
        password=password,
        database_url=settings.database_url,
        poll_seconds=settings.lalafo_auto_reply_poll_seconds,
    )


async def _restart_lalafo_auto_responder(reason: str) -> None:
    """Restart only the Lalafo worker without touching Telegram or payments."""
    global _lalafo_auto_responder
    async with _lalafo_restart_lock:
        current = _lalafo_auto_responder
        settings = get_settings()
        if current is not None and current.is_healthy(
            stale_after_seconds=settings.lalafo_auto_reply_stale_seconds
        ):
            return
        logger.error("Restarting Lalafo auto-reply worker: %s", reason)
        if current is not None:
            try:
                await asyncio.wait_for(current.close(), timeout=20.0)
            except TimeoutError:
                logger.error("Timed out while stopping stale Lalafo auto-reply worker")
            except Exception:
                logger.exception("Could not stop stale Lalafo auto-reply worker cleanly")
        replacement = _build_lalafo_auto_responder()
        replacement.start()
        _lalafo_auto_responder = replacement


async def _watch_lalafo_auto_responder() -> None:
    settings = get_settings()
    interval = max(10.0, settings.lalafo_auto_reply_watchdog_seconds)
    stale_after = max(60.0, settings.lalafo_auto_reply_stale_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            responder = _lalafo_auto_responder
            if responder is None:
                await _restart_lalafo_auto_responder("worker is missing")
            elif not responder.is_healthy(stale_after_seconds=stale_after):
                status_snapshot = responder.status(stale_after_seconds=stale_after)
                if status_snapshot.get("last_error") == "security_challenge":
                    # A Lalafo 403/CAPTCHA requires operator intervention. Keep
                    # the web process healthy without retrying the blocked login.
                    continue
                await _restart_lalafo_auto_responder("worker is stopped or stale")
        except asyncio.CancelledError:
            raise
        except Exception:
            # The watchdog itself must survive a failed restart and try again.
            logger.exception("Lalafo auto-reply watchdog cycle failed")


async def _select_hosted_lalafo_proxies() -> None:
    """Refresh cloud proxies before a due publication without touching auto-reply."""
    from scripts.select_lalafo_proxy import find_working_proxies

    settings = get_settings()
    for attempt in range(1, 4):
        try:
            selected = await find_working_proxies()
        except Exception:
            selected = []
            logger.exception("Hosted proxy selection attempt %d failed", attempt)
        if selected:
            settings.lalafo_proxy_url = ",".join(selected)
            logger.info("Hosted publisher selected %d verified proxies", len(selected))
            return
        if attempt < 3:
            await asyncio.sleep(attempt * 10)
    settings.lalafo_proxy_url = ""
    logger.warning("Hosted publisher found no proxy; direct Lalafo route will be tried")


async def _execute_due_apartment_cycle() -> int:
    async with _run_lock:
        settings = get_settings()
        _apartment_scheduler_state.update(
            running_cycle=True,
            last_check_at=_now(),
            last_error=None,
        )
        _run_state.update(
            running=True,
            last_started_at=_now(),
            last_finished_at=None,
            last_exit_code=None,
        )
        try:
            from scripts.publish_if_due import publication_schedule_status
            from scripts.publish_if_due import publication_window_status
            from scripts.publish_if_due import run as run_if_due

            schedule = await publication_schedule_status(
                window_minutes=settings.hosted_apartment_publish_interval_minutes
            )
            recent_count, latest_published_at = await publication_window_status(
                window_minutes=settings.hosted_apartment_publish_interval_minutes
            )
            _apartment_scheduler_state.update(
                recent_published_count=recent_count,
                latest_published_at=(
                    latest_published_at.isoformat() if latest_published_at else None
                ),
                schedule_status=schedule.status,
                schedule_due=schedule.due,
                schedule_last_started_at=(
                    schedule.last_started_at.isoformat()
                    if schedule.last_started_at
                    else None
                ),
                schedule_last_completed_at=(
                    schedule.last_completed_at.isoformat()
                    if schedule.last_completed_at
                    else None
                ),
            )
            if not schedule.due:
                _apartment_scheduler_state["last_exit_code"] = 0
                _run_state["last_exit_code"] = 0
                logger.info(
                    "Hosted scheduler skipped by shared clock: status=%s lease_active=%s "
                    "last_started_at=%s",
                    schedule.status,
                    schedule.lease_active,
                    schedule.last_started_at,
                )
                return 0

            await _select_hosted_lalafo_proxies()
            exit_code = await run_if_due(
                force=False,
                window_minutes=settings.hosted_apartment_publish_interval_minutes,
                max_attempts=3,
                wait_for_active_lease=False,
            )
            after_schedule = await publication_schedule_status(
                window_minutes=settings.hosted_apartment_publish_interval_minutes
            )
            after_count, after_latest = await publication_window_status(
                window_minutes=settings.hosted_apartment_publish_interval_minutes
            )
            _apartment_scheduler_state.update(
                recent_published_count=after_count,
                latest_published_at=after_latest.isoformat() if after_latest else None,
                schedule_status=after_schedule.status,
                schedule_due=after_schedule.due,
                schedule_last_started_at=(
                    after_schedule.last_started_at.isoformat()
                    if after_schedule.last_started_at
                    else None
                ),
                schedule_last_completed_at=(
                    after_schedule.last_completed_at.isoformat()
                    if after_schedule.last_completed_at
                    else None
                ),
                last_error=(
                    "NoApartmentsPublished"
                    if exit_code == 0 and after_count == 0
                    else None
                ),
            )
            _apartment_scheduler_state["last_exit_code"] = exit_code
            _run_state["last_exit_code"] = exit_code
            return exit_code
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _apartment_scheduler_state.update(
                last_exit_code=1,
                last_error=type(exc).__name__,
            )
            _run_state["last_exit_code"] = 1
            logger.exception("Hosted hourly apartment cycle failed")
            return 1
        finally:
            _apartment_scheduler_state["running_cycle"] = False
            _run_state.update(running=False, last_finished_at=_now())


async def _run_hosted_apartment_scheduler() -> None:
    settings = get_settings()
    check_seconds = max(30.0, settings.hosted_apartment_scheduler_check_seconds)
    while True:
        try:
            await _execute_due_apartment_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed cycle must never terminate the permanent scheduler.
            _apartment_scheduler_state["last_error"] = type(exc).__name__
            logger.exception("Hosted apartment scheduler recovered from a crash")
        await asyncio.sleep(check_seconds)


def _task_stopped(task: asyncio.Task[None] | None) -> bool:
    return task is None or task.done()


def _task_error_name(task: asyncio.Task[None] | None) -> str | None:
    if task is None or not task.done() or task.cancelled():
        return None
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return None
    return type(error).__name__ if error is not None else "UnexpectedTaskExit"


async def _repair_background_tasks_once() -> int:
    """Restart only stopped workers; never replace the payment runtime."""
    global _bot_setup_task, _lalafo_watchdog_task
    global _apartment_scheduler_task, _service_keepalive_task
    settings = get_settings()
    if _shutting_down:
        return 0
    restarted = 0

    if settings.run_bot and _task_stopped(_bot_setup_task):
        logger.error(
            "Restarting Telegram setup maintainer after %s",
            _task_error_name(_bot_setup_task) or "stop",
        )
        _bot_setup_task = asyncio.create_task(
            _configure_main_bot(), name="telegram-setup-maintainer"
        )
        restarted += 1

    if (
        settings.run_bot
        and settings.hosted_apartment_scheduler_enabled
        and _task_stopped(_apartment_scheduler_task)
    ):
        logger.error(
            "Restarting apartment scheduler after %s",
            _task_error_name(_apartment_scheduler_task) or "stop",
        )
        _apartment_scheduler_task = asyncio.create_task(
            _run_hosted_apartment_scheduler(), name="apartment-scheduler"
        )
        restarted += 1

    if (
        settings.lalafo_auto_reply_enabled
        and settings.lalafo_auto_reply_web_enabled
        and _task_stopped(_lalafo_watchdog_task)
    ):
        logger.error(
            "Restarting Lalafo watchdog after %s",
            _task_error_name(_lalafo_watchdog_task) or "stop",
        )
        _lalafo_watchdog_task = asyncio.create_task(
            _watch_lalafo_auto_responder(), name="lalafo-auto-reply-watchdog"
        )
        restarted += 1

    if (
        settings.run_bot
        and settings.service_keepalive_enabled
        and _task_stopped(_service_keepalive_task)
    ):
        logger.error(
            "Restarting free-cloud keepalive after %s",
            _task_error_name(_service_keepalive_task) or "stop",
        )
        _service_keepalive_task = asyncio.create_task(
            _keep_service_awake(), name="free-cloud-keepalive"
        )
        restarted += 1

    if restarted:
        _background_watchdog_state["restart_count"] = int(
            _background_watchdog_state.get("restart_count") or 0
        ) + restarted
    return restarted


async def _watch_background_tasks() -> None:
    interval = max(10.0, get_settings().background_watchdog_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            await _repair_background_tasks_once()
            _background_watchdog_state.update(
                state="running",
                last_check_at=_now(),
                last_error=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # This outer watchdog must itself survive every individual repair.
            _background_watchdog_state.update(
                state="recovering",
                last_check_at=_now(),
                last_error=type(exc).__name__,
            )
            logger.exception("Background watchdog cycle failed")


@app.on_event("startup")
async def startup() -> None:
    global _bot_runtime, _bot_setup_task, _legacy_featured_cleanup_task
    global _keyboard_sync_task, _lalafo_auto_responder
    global _lalafo_watchdog_task, _apartment_scheduler_task
    global _service_keepalive_task, _background_watchdog_task, _shutting_down
    settings = get_settings()
    _shutting_down = False
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every proxy probe at INFO. On the 0.1-vCPU free instance this
    # noise can consume more CPU than the actual recovery work.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings.require_run_trigger_secret()
    if settings.run_bot:
        # Validate configuration synchronously, but do not wait for Telegram
        # network calls before FastAPI starts accepting customer webhooks.
        settings.require_telegram_webhook_url()
        settings.require_telegram_webhook_secret()
        _bot_runtime = await create_runtime()
        _bot_setup_state.update(state="configuring", last_error=None)
        _bot_setup_task = asyncio.create_task(
            _configure_main_bot(), name="telegram-setup-maintainer"
        )
        logger.info("Telegram runtime ready; network setup continues in background")
        _keyboard_sync_task = asyncio.create_task(
            _sync_outdated_keyboards(), name="keyboard-sync"
        )
        if settings.hosted_apartment_scheduler_enabled:
            _apartment_scheduler_task = asyncio.create_task(
                _run_hosted_apartment_scheduler(), name="apartment-scheduler"
            )
            logger.info("Hosted hourly apartment scheduler enabled")
        if settings.service_keepalive_enabled:
            _service_keepalive_state.update(
                state="starting",
                last_error=None,
                consecutive_failures=0,
            )
            _service_keepalive_task = asyncio.create_task(
                _keep_service_awake(), name="free-cloud-keepalive"
            )
            logger.info("Free-cloud keepalive enabled")
    if settings.lalafo_auto_reply_enabled and settings.lalafo_auto_reply_web_enabled:
        _lalafo_auto_responder = _build_lalafo_auto_responder()
        _lalafo_auto_responder.start()
        _lalafo_watchdog_task = asyncio.create_task(
            _watch_lalafo_auto_responder(), name="lalafo-auto-reply-watchdog"
        )
        logger.info("Lalafo cloud auto-reply supervisor enabled")
    # One harmless cleanup call disconnects the old advertising bot.  No
    # advertising runtime, route, scheduler, or publisher is started anymore.
    _legacy_featured_cleanup_task = asyncio.create_task(
        _remove_legacy_featured_webhook(), name="retired-advertising-bot-cleanup"
    )
    _background_watchdog_state.update(state="starting", last_error=None)
    _background_watchdog_task = asyncio.create_task(
        _watch_background_tasks(), name="background-task-watchdog"
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    global _bot_runtime, _bot_setup_task, _legacy_featured_cleanup_task
    global _keyboard_sync_task, _lalafo_auto_responder
    global _lalafo_watchdog_task, _apartment_scheduler_task
    global _service_keepalive_task, _background_watchdog_task, _shutting_down
    _shutting_down = True
    if _background_watchdog_task is not None and not _background_watchdog_task.done():
        _background_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await _background_watchdog_task
    _background_watchdog_task = None
    if _service_keepalive_task is not None and not _service_keepalive_task.done():
        _service_keepalive_task.cancel()
        with suppress(asyncio.CancelledError):
            await _service_keepalive_task
    _service_keepalive_task = None
    if _bot_setup_task is not None and not _bot_setup_task.done():
        _bot_setup_task.cancel()
        with suppress(asyncio.CancelledError):
            await _bot_setup_task
    _bot_setup_task = None
    if (
        _legacy_featured_cleanup_task is not None
        and not _legacy_featured_cleanup_task.done()
    ):
        _legacy_featured_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await _legacy_featured_cleanup_task
    _legacy_featured_cleanup_task = None
    if _apartment_scheduler_task is not None and not _apartment_scheduler_task.done():
        _apartment_scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await _apartment_scheduler_task
    _apartment_scheduler_task = None
    if _lalafo_watchdog_task is not None and not _lalafo_watchdog_task.done():
        _lalafo_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await _lalafo_watchdog_task
    _lalafo_watchdog_task = None
    if _lalafo_auto_responder is not None:
        await _lalafo_auto_responder.close()
        _lalafo_auto_responder = None
    if _keyboard_sync_task is not None and not _keyboard_sync_task.done():
        _keyboard_sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keyboard_sync_task
    _keyboard_sync_task = None
    if _bot_runtime is not None:
        await _bot_runtime.close()
        _bot_runtime = None


@app.get("/health")
async def health() -> JSONResponse:
    settings = get_settings()
    if settings.run_bot and _bot_runtime is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "bot": "stopped"},
        )
    scheduler_running = (
        _apartment_scheduler_task is not None
        and not _apartment_scheduler_task.done()
    )
    auto_reply: dict[str, Any] | str = "disabled"
    if settings.lalafo_auto_reply_enabled and settings.lalafo_auto_reply_web_enabled:
        responder = _lalafo_auto_responder
        auto_reply = (
            responder.status(
                stale_after_seconds=settings.lalafo_auto_reply_stale_seconds
            )
            if responder is not None
            else {"state": "stopped"}
        )
        # The watchdog repairs this auxiliary worker.  Never make Koyeb restart
        # the customer/payment bot merely because Lalafo itself is unavailable.
    return JSONResponse(
        content={
            "status": "ok",
            "bot": "running" if settings.run_bot else "disabled",
            "telegram_setup": (
                dict(_bot_setup_state) if settings.run_bot else "disabled"
            ),
            "free_cloud_keepalive": (
                dict(_service_keepalive_state)
                if settings.run_bot and settings.service_keepalive_enabled
                else "disabled"
            ),
            "background_watchdog": dict(_background_watchdog_state),
            "lalafo_auto_reply": auto_reply,
            "apartment_scheduler": (
                {
                    "state": "running" if scheduler_running else "recovering",
                    **_apartment_scheduler_state,
                }
                if settings.run_bot and settings.hosted_apartment_scheduler_enabled
                else "disabled"
            ),
        }
    )


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    runtime = _bot_runtime
    if not settings.run_bot or runtime is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    expected = settings.require_telegram_webhook_secret()
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
    await runtime.dispatcher.feed_update(
        runtime.bot,
        update,
        **runtime.workflow_data,
    )
    return Response(status_code=status.HTTP_200_OK)


def _miniapp_context(payload: MiniAppRequest):
    settings = get_settings()
    runtime = _bot_runtime
    if not settings.run_bot or runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Сервис временно недоступен.",
        )
    user = verify_telegram_init_data(
        payload.init_data,
        bot_token=settings.require_bot_token(),
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось подтвердить пользователя Telegram.",
        )
    signer = TokenSigner(settings.require_callback_secret())
    apartment_id = signer.verify_start_id("miniapp-apartment", payload.start_param)
    if apartment_id is None:
        apartment_id = signer.decode_public_start_id(payload.start_param)
    if apartment_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ссылка на квартиру недействительна.",
        )
    return settings, runtime, user, apartment_id


def _miniapp_result_payload(result) -> dict[str, Any]:
    apartment = result.apartment
    if apartment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Квартира больше недоступна.",
        )
    formatted = format_apartment(apartment).splitlines()
    response: dict[str, Any] = {
        "status": result.status,
        "title": room_title(apartment.rooms),
        "details": "\n".join(formatted[1:]),
        "photo_url": apartment.photo_urls[0] if apartment.photo_urls else None,
        "price": WEEK_PRICE,
    }
    if result.status == "approved":
        response["phone"] = display_phone(apartment.phone)
        if result.access_expires_at is not None:
            local_expiry = result.access_expires_at.astimezone(ZoneInfo("Asia/Bishkek"))
            response["expires_at_text"] = local_expiry.strftime("%d.%m.%Y %H:%M")
    return response


@app.get("/miniapp", response_class=HTMLResponse, include_in_schema=False)
async def telegram_mini_app() -> HTMLResponse:
    return HTMLResponse(
        mini_app_html(),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src https: data:; "
                "connect-src 'self'; frame-ancestors https://web.telegram.org"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/miniapp/api/session", include_in_schema=False)
async def miniapp_session(payload: MiniAppRequest) -> dict[str, Any]:
    _, runtime, user, apartment_id = _miniapp_context(payload)
    result = await runtime.workflow_data["service"].contact_status(user.id, apartment_id)
    if result.status == "unavailable":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Квартира больше недоступна.",
        )
    return _miniapp_result_payload(result)


@app.post("/miniapp/api/start", include_in_schema=False)
async def miniapp_start_payment(payload: MiniAppRequest) -> dict[str, Any]:
    settings, runtime, user, apartment_id = _miniapp_context(payload)
    service = runtime.workflow_data["service"]
    result = await service.contact_status(user.id, apartment_id)
    if result.status not in {"approved", "pending", "awaiting_receipt"}:
        try:
            await service.begin_payment(
                user_id=user.id,
                apartment_id=apartment_id,
                username=user.username,
                first_name=user.first_name,
                plan=WEEK_PLAN,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Квартира больше недоступна.",
            ) from exc
        result = await service.contact_status(user.id, apartment_id)
    response = _miniapp_result_payload(result)
    response["payment_url"] = settings.finik_payment_url
    return response


@app.post("/miniapp/api/check", include_in_schema=False)
async def miniapp_check_payment(payload: MiniAppRequest) -> dict[str, Any]:
    settings, runtime, user, apartment_id = _miniapp_context(payload)
    if not settings.admin_user_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Проверка оплаты временно недоступна.",
        )
    service = runtime.workflow_data["service"]
    payments = runtime.workflow_data["payments"]
    current = await service.contact_status(user.id, apartment_id)
    if current.status == "approved":
        return _miniapp_result_payload(current)
    if current.status == "awaiting_receipt":
        request = await payments.mark_payment_claimed(
            user_id=user.id,
            apartment_id=apartment_id,
        )
    elif current.status == "pending":
        # A repeated tap is harmless and can repair a previously failed admin
        # notification without creating a second payment request.
        request = await payments.get_access(user.id, apartment_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала откройте ссылку на оплату.",
        )
    if request is None:
        raise HTTPException(status_code=409, detail="Сначала откройте оплату.")
    if await payments.claim_admin_notification(request.id):
        try:
            admin_message = await runtime.bot.send_message(
                settings.admin_user_id,
                format_admin_card(request),
                reply_markup=admin_keyboard(
                    request.id,
                    signer=TokenSigner(settings.require_callback_secret()),
                ),
            )
        except Exception as exc:
            await payments.release_admin_notification(request.id)
            logger.exception("Mini App payment notification failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Не удалось отправить оплату на проверку. Попробуйте ещё раз.",
            ) from exc
        await payments.finish_admin_notification(request.id, admin_message.message_id)
    result = await service.contact_status(user.id, apartment_id)
    return _miniapp_result_payload(result)


@app.post("/miniapp/api/receipt", include_in_schema=False)
async def miniapp_upload_receipt(payload: MiniAppReceiptRequest) -> dict[str, Any]:
    settings, runtime, user, apartment_id = _miniapp_context(payload)
    if not settings.admin_user_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Проверка оплаты временно недоступна.",
        )
    if len(payload.file_base64) > 14_000_000:
        raise HTTPException(status_code=413, detail="Файл должен быть не больше 10 МБ.")
    try:
        file_bytes = base64.b64decode(payload.file_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="Не удалось прочитать файл чека.") from exc
    if not file_bytes or len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл должен быть не больше 10 МБ.")
    allowed_types = {"image/jpeg", "image/png", "application/pdf"}
    content_type = payload.content_type.casefold()
    if content_type not in allowed_types:
        raise HTTPException(
            status_code=415,
            detail="Прикрепите чек в формате JPG, PNG или PDF.",
        )

    service = runtime.workflow_data["service"]
    payments = runtime.workflow_data["payments"]
    current = await service.contact_status(user.id, apartment_id)
    if current.status == "approved":
        return _miniapp_result_payload(current)
    if current.status == "pending":
        return _miniapp_result_payload(current)
    if current.status != "awaiting_receipt":
        try:
            await service.begin_payment(
                user_id=user.id,
                apartment_id=apartment_id,
                username=user.username,
                first_name=user.first_name,
                plan=WEEK_PLAN,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Квартира больше недоступна.",
            ) from exc

    request = await service.submit_receipt(
        user_id=user.id,
        file_id=f"miniapp:{apartment_id}:{int(datetime.now(UTC).timestamp())}",
        file_type="photo" if content_type.startswith("image/") else "document",
    )
    if request is None:
        raise HTTPException(status_code=409, detail="Сначала откройте оплату.")
    if not await payments.claim_admin_notification(request.id):
        result = await service.contact_status(user.id, apartment_id)
        return _miniapp_result_payload(result)

    filename = PurePath(payload.file_name or "receipt").name[:120] or "receipt"
    upload = BufferedInputFile(file_bytes, filename=filename)
    try:
        caption = format_admin_card(request)
        markup = admin_keyboard(request.id, signer=TokenSigner(settings.require_callback_secret()))
        if content_type.startswith("image/"):
            admin_message = await runtime.bot.send_photo(
                settings.admin_user_id,
                upload,
                caption=caption,
                reply_markup=markup,
            )
        else:
            admin_message = await runtime.bot.send_document(
                settings.admin_user_id,
                upload,
                caption=caption,
                reply_markup=markup,
            )
    except Exception as exc:
        await payments.restore_receipt_upload(request.id)
        logger.exception("Mini App receipt notification failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось отправить чек. Попробуйте ещё раз.",
        ) from exc
    await payments.finish_admin_notification(request.id, admin_message.message_id)
    result = await service.contact_status(user.id, apartment_id)
    return _miniapp_result_payload(result)


@app.get("/pay/{token}", include_in_schema=False)
async def open_finik_payment(token: str) -> RedirectResponse:
    settings = get_settings()
    runtime = _bot_runtime
    if not settings.run_bot or runtime is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    signer = TokenSigner(settings.require_callback_secret())
    values = signer.verify_values("finik-redirect", token, count=3)
    if values is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    apartment_id, chat_id, message_id = values
    try:
        await runtime.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=paid_keyboard(
                apartment_id,
                signer=signer,
                support_url=settings.support_bot_url,
            ),
        )
    except Exception:
        logger.exception("Could not replace payment button with paid confirmation")
    return RedirectResponse(settings.finik_payment_url, status_code=status.HTTP_302_FOUND)


@app.get("/status")
async def scraper_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(authorization)
    return dict(_run_state)


@app.post("/run")
async def trigger_scraper(authorization: str | None = Header(default=None)) -> dict[str, str | int]:
    _authorize(authorization)
    if _run_lock.locked():
        return {"status": "already_running"}
    exit_code = await _execute_scraper()
    return {"status": "completed", "exit_code": exit_code}

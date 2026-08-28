from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.bot.main import BotRuntime, create_runtime
from app.config import get_settings
from app.lalafo.auto_reply import LalafoAutoResponder
from app.security import TokenSigner
from app.telegram.keyboards import paid_keyboard

logger = logging.getLogger(__name__)
app = FastAPI(title="Lalafo Telegram service", docs_url=None, redoc_url=None)

_run_lock = asyncio.Lock()
_scraper_task: asyncio.Task[None] | None = None
_bot_runtime: BotRuntime | None = None
_keyboard_sync_task: asyncio.Task[None] | None = None
_lalafo_auto_responder: LalafoAutoResponder | None = None
_lalafo_watchdog_task: asyncio.Task[None] | None = None
_lalafo_restart_lock = asyncio.Lock()
_apartment_scheduler_task: asyncio.Task[None] | None = None
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
            logger.exception("Hosted two-hour apartment cycle failed")
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


@app.on_event("startup")
async def startup() -> None:
    global _bot_runtime, _keyboard_sync_task, _lalafo_auto_responder
    global _lalafo_watchdog_task, _apartment_scheduler_task
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.require_run_trigger_secret()
    if settings.run_bot:
        webhook_url = settings.require_telegram_webhook_url()
        webhook_secret = settings.require_telegram_webhook_secret()
        _bot_runtime = await create_runtime()
        await _bot_runtime.bot.set_webhook(
            webhook_url,
            secret_token=webhook_secret,
            allowed_updates=_bot_runtime.dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook enabled at %s", webhook_url)
        _keyboard_sync_task = asyncio.create_task(_sync_outdated_keyboards())
        if settings.hosted_apartment_scheduler_enabled:
            _apartment_scheduler_task = asyncio.create_task(
                _run_hosted_apartment_scheduler()
            )
            logger.info("Hosted two-hour apartment scheduler enabled")
    if settings.lalafo_auto_reply_enabled:
        _lalafo_auto_responder = _build_lalafo_auto_responder()
        _lalafo_auto_responder.start()
        _lalafo_watchdog_task = asyncio.create_task(_watch_lalafo_auto_responder())
        logger.info("Lalafo cloud auto-reply supervisor enabled")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _bot_runtime, _keyboard_sync_task, _lalafo_auto_responder
    global _lalafo_watchdog_task, _apartment_scheduler_task
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
    if settings.run_bot and settings.hosted_apartment_scheduler_enabled:
        if _apartment_scheduler_task is None or _apartment_scheduler_task.done():
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "bot": "running",
                    "apartment_scheduler": {
                        "state": "stopped",
                        **_apartment_scheduler_state,
                    },
                },
            )
    auto_reply: dict[str, Any] | str = "disabled"
    if settings.lalafo_auto_reply_enabled:
        responder = _lalafo_auto_responder
        auto_reply = (
            responder.status(
                stale_after_seconds=settings.lalafo_auto_reply_stale_seconds
            )
            if responder is not None
            else {"state": "stopped"}
        )
        if responder is None or not responder.is_healthy(
            stale_after_seconds=settings.lalafo_auto_reply_stale_seconds
        ):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "bot": "running" if settings.run_bot else "disabled",
                    "lalafo_auto_reply": auto_reply,
                },
            )
    return JSONResponse(
        content={
            "status": "ok",
            "bot": "running" if settings.run_bot else "disabled",
            "lalafo_auto_reply": auto_reply,
            "apartment_scheduler": (
                {"state": "running", **_apartment_scheduler_state}
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
                support_url=settings.support_url,
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

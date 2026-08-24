from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.bot.main import BotRuntime, create_runtime
from app.config import get_settings
from app.security import TokenSigner
from app.telegram.keyboards import paid_keyboard

logger = logging.getLogger(__name__)
app = FastAPI(title="Lalafo Telegram service", docs_url=None, redoc_url=None)

_run_lock = asyncio.Lock()
_scraper_task: asyncio.Task[None] | None = None
_bot_runtime: BotRuntime | None = None
_run_state: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
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


@app.on_event("startup")
async def startup() -> None:
    global _bot_runtime
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
        try:
            from scripts.sync_published_keyboards import run as sync_published_keyboards

            sync_exit_code = await sync_published_keyboards()
            if sync_exit_code != 0:
                logger.warning(
                    "Published keyboard startup sync exited with code %d",
                    sync_exit_code,
                )
        except Exception:
            # A temporary Telegram failure must not prevent the webhook service
            # from starting. The scheduled cloud job will retry the same sync.
            logger.exception("Published keyboard startup sync failed")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _bot_runtime
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
    return JSONResponse(
        content={
            "status": "ok",
            "bot": "running" if settings.run_bot else "disabled",
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
            reply_markup=paid_keyboard(apartment_id, signer=signer),
        )
    except Exception:
        logger.exception("Could not replace Finik button with paid confirmation")
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

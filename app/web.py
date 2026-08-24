from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)
app = FastAPI(title="Lalafo Telegram service", docs_url=None, redoc_url=None)

_run_lock = asyncio.Lock()
_scraper_task: asyncio.Task[None] | None = None
_bot_task: asyncio.Task[None] | None = None
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
    global _bot_task
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.require_run_trigger_secret()
    if settings.run_bot:
        from app.bot.main import run as run_bot

        _bot_task = asyncio.create_task(run_bot(), name="telegram-bot")


@app.on_event("shutdown")
async def shutdown() -> None:
    global _bot_task
    if _bot_task is not None:
        _bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _bot_task
        _bot_task = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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

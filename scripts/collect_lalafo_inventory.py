from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository
from scripts.inventory_cycle import discovery_outcome
from scripts.scrape_publish import (
    DISCOVERY_CANDIDATE_TARGET,
    DISCOVERY_COLLECTION_BUDGET_SECONDS,
    run as discover,
)
from scripts.select_lalafo_proxy import find_working_proxies

logger = logging.getLogger(__name__)
REPORT_PREFIX = "LALAFO_COLLECTION_RESULT "


async def collect(settings, inventory, *, force: bool = False) -> dict:
    """Refill the primary source using the same lease as GitHub collectors."""
    now = datetime.now(timezone.utc)
    key = await inventory.claim_discovery(now=now, lease_minutes=15, force=force)
    if key is None:
        return {"status": "not_due", "exit_code": 0}
    code = 1
    error = None
    routes = 0
    discovered_count = queued_count = 0
    stats: dict[str, Any] = {"stored": 0, "search_pages": 0, "search_results": 0,
                             "owner_candidates": 0, "owner_search_pages": 0}
    try:
        try:
            async with asyncio.timeout(45):
                selected = await find_working_proxies(settings.lalafo_proxy_url)
        except Exception:
            selected = []
            logger.exception("Lalafo route selection failed; keeping configured route")
        if selected:
            settings.lalafo_proxy_url = ",".join(selected)
            routes = len(selected)
        logger.info("Primary Lalafo collection starting verified_routes=%d", routes)
        # Keep larger searches within the small cloud instance's memory.
        settings.apartment_detail_concurrency = min(2, settings.apartment_detail_concurrency)
        async with asyncio.timeout(300):
            code = await discover(
                discovery_only=True,
                candidate_pool_limit_override=DISCOVERY_CANDIDATE_TARGET,
                include_telegram_sources=False,
                discovery_stats=stats,
                collection_budget_seconds=DISCOVERY_COLLECTION_BUDGET_SECONDS,
            )
        if code == 0 and stats["search_pages"] == 0:
            code = 2
            error = "NoSourcePagesRead"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = type(exc).__name__
        logger.exception("Primary Lalafo collection failed; saved inventory retained")
    finally:
        source_error = stats.get("last_source_error")
        if code != 0 and error is None and isinstance(source_error, dict):
            error = str(source_error["type"])
            if source_error.get("http_status") is not None:
                error += f":HTTP{source_error['http_status']}"
        discovered_count, queued_count = await inventory.period_counts(now=now)
        published_count = await inventory.period_published_count(now=now)
        success, outcome_error = discovery_outcome(exit_code=code, queued_count=queued_count, published_count=published_count)
        await inventory.finish_discovery(
            key, success=success and error is None,
            discovered=discovered_count, queued=queued_count,
            error=error or outcome_error,
        )
    return {
        "status": "collected" if code == 0 and error is None else "retryable",
        "exit_code": code, "error": error or (outcome_error if code != 0 else None),
        "inventory_error": outcome_error,
        "verified_routes": routes, "recent_inventory": discovered_count,
        "period_queue": queued_count,
        "period_published": published_count,
        **stats,
    }


async def run() -> int:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        result = await collect(settings, InventoryRepository(sessions),
            force=os.getenv("FORCE_DISCOVERY", "").strip().casefold() in {"1", "true", "yes", "on"})
        print(REPORT_PREFIX + json.dumps(result), flush=True)
        return result["exit_code"]
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository
from scripts.inventory_cycle import discovery_outcome
from scripts.scrape_publish import run as discover
from scripts.select_lalafo_proxy import find_working_proxies

logger = logging.getLogger(__name__)
REPORT_PREFIX = "LALAFO_COLLECTION_RESULT "


async def collect(settings, inventory) -> dict:
    """Refill the primary source using the same lease as GitHub collectors."""
    now = datetime.now(timezone.utc)
    key = await inventory.claim_discovery(now=now)
    if key is None:
        return {"status": "not_due", "exit_code": 0}
    code = 1
    error = None
    routes = 0
    discovered_count = queued_count = 0
    stats: dict[str, int] = {"stored": 0, "search_pages": 0, "search_results": 0}
    try:
        try:
            selected = await find_working_proxies()
        except Exception:
            selected = []
            logger.exception("Lalafo route selection failed; keeping configured route")
        if selected:
            settings.lalafo_proxy_url = ",".join(selected)
            routes = len(selected)
        logger.info("Primary Lalafo collection starting verified_routes=%d", routes)
        async with asyncio.timeout(600):
            code = await discover(
                discovery_only=True,
                candidate_pool_limit_override=20,
                include_telegram_sources=False,
                discovery_stats=stats,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = type(exc).__name__
        logger.exception("Primary Lalafo collection failed; saved inventory retained")
    finally:
        discovered_count, queued_count = await inventory.period_counts(now=now)
        success, outcome_error = discovery_outcome(exit_code=code, queued_count=queued_count)
        await inventory.finish_discovery(
            key, success=success and error is None,
            discovered=discovered_count, queued=queued_count,
            error=error or outcome_error,
        )
    return {
        "status": "collected" if code == 0 and error is None else "retryable",
        "exit_code": code, "error": error or outcome_error,
        "verified_routes": routes, "recent_inventory": discovered_count,
        "period_queue": queued_count,
        **stats,
    }


async def run() -> int:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        result = await collect(settings, InventoryRepository(sessions))
        print(REPORT_PREFIX + json.dumps(result), flush=True)
        return result["exit_code"]
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository
from scripts.publish_inventory import run as publish_one_due
from scripts.scrape_publish import run as discover


logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


async def run(*, force_discovery: bool | None = None) -> int:
    """Run the due 12-hour search and dispatch no more than one due card."""
    settings = get_settings()
    force = _truthy(os.getenv("FORCE_DISCOVERY")) if force_discovery is None else force_discovery
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)
    period_key = await inventory.claim_discovery(now=now, force=force)
    await engine.dispose()

    if period_key is not None:
        success = False
        error = None
        try:
            code = await discover(discovery_only=True)
            success = code == 0
            if not success:
                error = f"ExitCode{code}"
        except Exception as exc:
            code = 1
            error = type(exc).__name__
            logger.exception("Large apartment discovery failed; existing inventory retained")
        engine, sessions = create_engine_and_session(settings.database_url)
        try:
            repository = InventoryRepository(sessions)
            discovered_count, queued_count = await repository.period_counts(now=now)
            await repository.finish_discovery(
                period_key,
                success=success,
                discovered=discovered_count,
                queued=queued_count,
                error=error,
            )
        finally:
            await engine.dispose()

    return await publish_one_due()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

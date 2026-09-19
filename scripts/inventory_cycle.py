from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository, MIN_HEALTHY_PERIOD_QUEUE
from scripts.publish_inventory import run as publish_one_due
from scripts.scrape_publish import run as discover


logger = logging.getLogger(__name__)
def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def discovery_outcome(*, exit_code: int, queued_count: int) -> tuple[bool, str | None]:
    """Only a populated period is a successful large discovery.

    A clean scraper exit with an empty queue used to suppress every retry for
    the rest of the 12-hour period.  Keep it retryable instead.
    """
    if exit_code != 0:
        return False, f"ExitCode{exit_code}"
    if queued_count <= 0:
        return False, "EmptyInventory"
    if queued_count < MIN_HEALTHY_PERIOD_QUEUE:
        return False, "ThinInventory"
    return True, None


async def run(*, force_discovery: bool | None = None) -> int:
    """Run the due 12-hour search and dispatch no more than one due card."""
    settings = get_settings()
    force = (
        _truthy(os.getenv("FORCE_DISCOVERY"))
        or _truthy(os.getenv("FORCE_PUBLISH"))
        if force_discovery is None
        else force_discovery
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    inventory = InventoryRepository(sessions)
    now = datetime.now(timezone.utc)
    period_key = await inventory.claim_discovery(now=now, force=force)
    await engine.dispose()

    if period_key is not None:
        code = 1
        raised_error = None
        try:
            code = await discover(discovery_only=True)
        except Exception as exc:
            raised_error = type(exc).__name__
            logger.exception("Large apartment discovery failed; existing inventory retained")
        engine, sessions = create_engine_and_session(settings.database_url)
        try:
            repository = InventoryRepository(sessions)
            discovered_count, queued_count = await repository.period_counts(now=now)
            success, error = discovery_outcome(
                exit_code=code,
                queued_count=queued_count,
            )
            if raised_error is not None:
                success, error = False, raised_error
            await repository.finish_discovery(
                period_key,
                success=success,
                discovered=discovered_count,
                queued=queued_count,
                error=error,
            )
            if not success:
                logger.warning(
                    "Apartment discovery remains retryable: error=%s discovered=%d queued=%d",
                    error,
                    discovered_count,
                    queued_count,
                )
        finally:
            await engine.dispose()

    return await publish_one_due()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()


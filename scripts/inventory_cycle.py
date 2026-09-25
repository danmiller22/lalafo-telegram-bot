from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os

from app.availability import AvailabilityService, MAX_LISTING_AGE_DAYS
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository, MIN_HEALTHY_PERIOD_QUEUE
from app.payments.repository import ApartmentRepository
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
    code_version = (os.getenv("GITHUB_SHA") or "").strip()
    if await inventory.reset_publication_history_for_code(code_version):
        force = True
        logger.info(
            "Publication history reset for code version %s; old Telegram messages were untouched",
            code_version[:12],
        )

    # On a new code version, publish from validated saved stock before the
    # longer availability sweep and source collection. This makes the first
    # new card visible within the first scheduler minute.
    early_publish_code: int | None = None
    if force:
        await inventory.schedule_period(now=now)
        early_publish_code = await publish_one_due(
            eligible_until=now + timedelta(days=1)
        )

    if await inventory.claim_availability_sweep(now=now, interval_hours=12):
        apartments = ApartmentRepository(sessions)
        cutoff = now - timedelta(days=MAX_LISTING_AGE_DAYS)
        expired = await apartments.expire_published_before(
            cutoff=cutoff, checked_at=now
        )
        apartment_ids = await apartments.availability_candidates(
            published_since=cutoff
        )
        availability = AvailabilityService(apartments, settings)
        semaphore = asyncio.Semaphore(8)

        async def check_one(apartment_id: int):
            async with semaphore:
                return await availability.check(apartment_id, force=True)

        results = await asyncio.gather(
            *(check_one(apartment_id) for apartment_id in apartment_ids),
            return_exceptions=True,
        )
        checked = sum(not isinstance(result, Exception) for result in results)
        failed = len(results) - checked
        logger.info(
            "Availability sweep completed checked=%d expired=%d failed=%d",
            checked,
            expired,
            failed,
        )

    # Schedule existing stock first, but never let that suppress a requested
    # cloud collection.  The previous early return made a manual "collect now"
    # run publish one saved card without actually refreshing either source.
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

    # A forced recovery must not finish green with an empty queue.  When no
    # saved stock existed above, it reaches discovery and then publishes the
    # first newly scheduled card immediately instead of waiting for its slot.
    if early_publish_code is not None:
        return early_publish_code
    return await publish_one_due()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

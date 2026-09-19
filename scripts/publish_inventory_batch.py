from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.models import Apartment, ApartmentInventoryQueue
from scripts.inventory_cycle import run as run_inventory_cycle
from scripts.publish_inventory import run as publish_one


logger = logging.getLogger(__name__)


async def _published_since(started_at: datetime) -> int:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        async with sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at.is_not(None),
                        Apartment.published_at >= started_at,
                    )
                )
                or 0
            )
    finally:
        await engine.dispose()


async def _has_due_queue(now: datetime) -> bool:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        async with sessions() as session:
            return bool(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .where(
                        ApartmentInventoryQueue.status == "queued",
                        ApartmentInventoryQueue.scheduled_at <= now,
                    )
                )
            )
    finally:
        await engine.dispose()


async def run() -> int:
    """Publish six valid cards per cloud run, spaced to avoid a burst."""
    target = max(1, min(12, int(os.getenv("PUBLISH_BATCH_SIZE", "6"))))
    spacing = max(0, min(900, int(os.getenv("PUBLISH_BATCH_SPACING_SECONDS", "480"))))
    max_attempts = target * 4
    started_at = datetime.now(timezone.utc)
    eligible_until = started_at + timedelta(hours=2)
    had_due_queue = await _has_due_queue(started_at)
    forced = os.getenv("FORCE_PUBLISH", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # This performs the due large discovery once and may publish the first due
    # card. Further iterations consume the same two-hour persisted window.
    code = await run_inventory_cycle()
    published = await _published_since(started_at)
    attempts = 1

    # The GitHub clock may tick more often than the public two-hour cadence.
    # Drain a complete window only when it was actually due, or when the owner
    # explicitly requests a manual launch.
    if not had_due_queue and not forced and published == 0:
        logger.info("No two-hour publication window is due")
        return code

    if published and published < target and spacing:
        logger.info(
            "Published %d/%d cards; waiting %d seconds before the next card",
            published,
            target,
            spacing,
        )
        await asyncio.sleep(spacing)

    while published < target and attempts < max_attempts:
        before = published
        result = await publish_one(eligible_until=eligible_until)
        code = max(code, result)
        attempts += 1
        published = await _published_since(started_at)
        if published > before and published < target and spacing:
            logger.info(
                "Published %d/%d cards; waiting %d seconds before the next card",
                published,
                target,
                spacing,
            )
            await asyncio.sleep(spacing)

    if published < target:
        logger.warning(
            "Batch finished with %d/%d cards after %d attempts; discovery will retry",
            published,
            target,
            attempts,
        )
    else:
        logger.info("Completed apartment batch: %d/%d", published, target)
    return code


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

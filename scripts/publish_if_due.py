from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment
from scripts.scrape_publish import run as run_scraper


logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def should_publish(*, force: bool, recent_count: int) -> bool:
    return force or recent_count == 0


async def recent_published_count(*, window_minutes: int) -> int:
    count, _ = await publication_window_status(window_minutes=window_minutes)
    return count


async def publication_window_status(
    *, window_minutes: int
) -> tuple[int, datetime | None]:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, window_minutes))
    try:
        async with sessions() as session:
            recent_count_query = (
                select(func.count())
                .select_from(Apartment)
                .where(
                    Apartment.publication_status == "published",
                    Apartment.published_at.is_not(None),
                    Apartment.published_at >= cutoff,
                )
                .scalar_subquery()
            )
            latest_query = (
                select(func.max(Apartment.published_at))
                .where(
                    Apartment.publication_status == "published",
                    Apartment.published_at.is_not(None),
                )
                .scalar_subquery()
            )
            result = await session.execute(
                select(recent_count_query, latest_query)
            )
            count, latest = result.one()
            return int(count or 0), latest
    finally:
        await engine.dispose()


async def run(
    *,
    force: bool | None = None,
    window_minutes: int | None = None,
    max_attempts: int | None = None,
) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    force = _truthy(os.getenv("FORCE_PUBLISH")) if force is None else force
    window_minutes = (
        max(1, int(os.getenv("PUBLISH_RECENT_WINDOW_MINUTES", "100")))
        if window_minutes is None
        else max(1, window_minutes)
    )
    max_attempts = (
        max(1, min(5, int(os.getenv("PUBLISH_MAX_ATTEMPTS", "3"))))
        if max_attempts is None
        else max(1, min(5, max_attempts))
    )
    recent_count = await recent_published_count(window_minutes=window_minutes)
    if not should_publish(force=force, recent_count=recent_count):
        logger.info(
            "Fresh publication detected: %d cards in the last %d minutes; "
            "this backup window is skipped",
            recent_count,
            window_minutes,
        )
        return 0

    last_code = 1
    for attempt in range(1, max_attempts + 1):
        logger.info("Starting publication cycle attempt %d/%d", attempt, max_attempts)
        try:
            last_code = await run_scraper()
        except Exception:
            last_code = 1
            logger.exception("Publication cycle attempt %d crashed", attempt)
        if last_code == 0:
            return 0
        if attempt < max_attempts:
            wait_seconds = 20 * attempt
            logger.warning(
                "Publication cycle exited with code %d; retrying in %d seconds",
                last_code,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
    logger.error("Publication failed after %d full-cycle attempts", max_attempts)
    return last_code


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

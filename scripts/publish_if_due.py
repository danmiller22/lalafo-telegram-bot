from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.models import Apartment
from app.publication_schedule import schedule_snapshot


logger = logging.getLogger(__name__)


def should_publish(*, force: bool, recent_count: int) -> bool:
    return force or recent_count == 0


def cycle_succeeded(*, exit_code: int, error: str | None, published: int) -> bool:
    """Treat a clean no-op as success when no fresh unique cards exist."""
    return exit_code == 0 or (published > 0 and error == "CycleTimeout")


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


async def published_since_count(
    sessions, *, started_at: datetime
) -> int:
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


async def publication_schedule_status(*, window_minutes: int):
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        # The durable inventory contains exact five-minute card times, so the
        # dispatcher must wake every minute. ``window_minutes`` remains in the
        # public signature for compatibility with older hosted callers.
        return await schedule_snapshot(sessions, interval_minutes=1)
    finally:
        await engine.dispose()


async def run(
    *,
    force: bool | None = None,
    window_minutes: int | None = None,
    max_attempts: int | None = None,
    wait_for_active_lease: bool | None = None,
) -> int:
    """Dispatch the durable inventory; legacy keyword arguments remain accepted."""
    from scripts.publish_inventory_batch import run as run_inventory_batch

    return await run_inventory_batch()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

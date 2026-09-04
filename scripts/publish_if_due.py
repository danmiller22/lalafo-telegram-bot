from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.models import Apartment
from app.publication_schedule import (
    claim_publication,
    finish_publication,
    publication_heartbeat,
    schedule_snapshot,
    stop_heartbeat,
)
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
        return await schedule_snapshot(sessions, interval_minutes=window_minutes)
    finally:
        await engine.dispose()


async def run(
    *,
    force: bool | None = None,
    window_minutes: int | None = None,
    max_attempts: int | None = None,
    wait_for_active_lease: bool | None = None,
) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    force = _truthy(os.getenv("FORCE_PUBLISH")) if force is None else force
    window_minutes = (
        settings.hosted_apartment_publish_interval_minutes
        if window_minutes is None
        else max(1, window_minutes)
    )
    max_attempts = (
        max(1, min(5, int(os.getenv("PUBLISH_MAX_ATTEMPTS", "3"))))
        if max_attempts is None
        else max(1, min(5, max_attempts))
    )
    wait_for_active_lease = (
        _truthy(os.getenv("PUBLISH_WAIT_FOR_ACTIVE_LEASE", "true"))
        if wait_for_active_lease is None
        else wait_for_active_lease
    )
    follower_wait_seconds = max(
        0, min(900, int(os.getenv("PUBLISH_FOLLOWER_WAIT_SECONDS", "600")))
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        claim = await claim_publication(
            sessions,
            force=force,
            interval_minutes=window_minutes,
            lease_seconds=settings.apartment_publication_lease_seconds,
        )
    except Exception:
        await engine.dispose()
        raise
    if claim is None and wait_for_active_lease and follower_wait_seconds:
        deadline = asyncio.get_running_loop().time() + follower_wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await schedule_snapshot(
                sessions, interval_minutes=window_minutes
            )
            if snapshot.status == "succeeded" and not snapshot.due:
                logger.info("The active publisher completed successfully; follower exits")
                await engine.dispose()
                return 0
            if snapshot.due and not snapshot.lease_active:
                claim = await claim_publication(
                    sessions,
                    force=False,
                    interval_minutes=window_minutes,
                    lease_seconds=settings.apartment_publication_lease_seconds,
                )
                if claim is not None:
                    logger.warning(
                        "The previous publisher failed; follower claimed cycle token=%s",
                        claim.token[:8],
                    )
                    break
            await asyncio.sleep(20)

    if claim is None:
        try:
            snapshot = await schedule_snapshot(
                sessions, interval_minutes=window_minutes
            )
            logger.info(
                "Publication skipped by shared clock: status=%s due=%s lease_active=%s "
                "last_started_at=%s",
                snapshot.status,
                snapshot.due,
                snapshot.lease_active,
                snapshot.last_started_at,
            )
        finally:
            await engine.dispose()
        return 0

    heartbeat = asyncio.create_task(
        publication_heartbeat(
            sessions,
            token=claim.token,
            lease_seconds=settings.apartment_publication_lease_seconds,
            heartbeat_seconds=settings.apartment_publication_heartbeat_seconds,
        )
    )
    last_code = 1
    error: str | None = None
    published = 0
    try:
        published = await published_since_count(
            sessions, started_at=claim.started_at
        )
        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Starting claimed publication cycle attempt %d/%d token=%s",
                attempt,
                max_attempts,
                claim.token[:8],
            )
            try:
                last_code = await asyncio.wait_for(
                    run_scraper(),
                    timeout=max(60.0, settings.apartment_cycle_timeout_seconds),
                )
                error = None if last_code == 0 else f"ExitCode{last_code}"
            except asyncio.TimeoutError:
                last_code = 2
                error = "CycleTimeout"
                logger.exception("Publication cycle exceeded its hard deadline")
            except Exception as exc:
                last_code = 1
                error = type(exc).__name__
                logger.exception("Publication cycle attempt %d crashed", attempt)

            published = await published_since_count(
                sessions, started_at=claim.started_at
            )
            # A hard timeout may cancel only the unfinished cards. Already
            # acknowledged cards are durable and must not be replaced by a
            # second oversized batch.
            if published > 0 and (last_code == 0 or error == "CycleTimeout"):
                await finish_publication(
                    sessions,
                    token=claim.token,
                    success=True,
                    published_count=published,
                    error=None,
                )
                return 0
            if attempt < max_attempts:
                wait_seconds = min(30, 10 * attempt)
                logger.warning(
                    "Publication produced no durable cards (code=%d); retrying in %d seconds",
                    last_code,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)

        error = error or "NoApartmentsPublished"
        await finish_publication(
            sessions,
            token=claim.token,
            success=False,
            published_count=published,
            error=error,
        )
        logger.error(
            "Publication failed after %d attempts without durable Telegram cards",
            max_attempts,
        )
        return last_code or 2
    finally:
        await stop_heartbeat(heartbeat)
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

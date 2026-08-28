from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Apartment, ApartmentPublicationSchedule


SCHEDULE_ID = 1
_BATCH_GAP = timedelta(minutes=45)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PublicationClaim:
    token: str
    started_at: datetime


@dataclass(frozen=True)
class PublicationScheduleSnapshot:
    status: str
    due: bool
    lease_active: bool
    last_started_at: datetime | None
    last_completed_at: datetime | None
    last_published_count: int
    last_error: str | None


async def _bootstrap_started_at(session: AsyncSession) -> datetime | None:
    """Recover the start of the latest contiguous batch for first deployment."""
    values = list(
        (
            await session.scalars(
                select(Apartment.published_at)
                .where(Apartment.published_at.is_not(None))
                .order_by(Apartment.published_at.desc())
                .limit(500)
            )
        ).all()
    )
    timestamps = [_aware(value) for value in values if value is not None]
    if not timestamps:
        return None
    batch_start = timestamps[0]
    previous = timestamps[0]
    for value in timestamps[1:]:
        if previous - value > _BATCH_GAP:
            break
        batch_start = value
        previous = value
    return batch_start


async def ensure_schedule_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        started_at = await _bootstrap_started_at(session)
        now = _utcnow()
        # PostgreSQL and the SQLite version used by tests both support this
        # idempotent singleton insert. Concurrent cloud workers cannot create
        # two independent clocks.
        await session.execute(
            text(
                """
                INSERT INTO apartment_publication_schedule
                    (id, status, last_started_at, last_completed_at,
                     last_published_count, updated_at)
                VALUES
                    (:id, :status, :started_at, :completed_at, 0, :updated_at)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": SCHEDULE_ID,
                "status": "succeeded" if started_at else "idle",
                "started_at": started_at,
                "completed_at": now if started_at else None,
                "updated_at": now,
            },
        )
        await session.commit()


def _state(
    row: ApartmentPublicationSchedule,
    *,
    now: datetime,
    interval_minutes: int,
) -> tuple[bool, bool]:
    lease_until = _aware(row.lease_until)
    lease_active = row.status == "running" and bool(lease_until and lease_until > now)
    last_started = _aware(row.last_started_at)
    interval_due = last_started is None or last_started <= now - timedelta(
        minutes=max(1, interval_minutes)
    )
    recovery_due = row.status in {"running", "failed"} and not bool(
        lease_until and lease_until > now
    )
    return interval_due or recovery_due, lease_active


async def schedule_snapshot(
    sessions: async_sessionmaker[AsyncSession], *, interval_minutes: int
) -> PublicationScheduleSnapshot:
    await ensure_schedule_row(sessions)
    async with sessions() as session:
        row = await session.get(ApartmentPublicationSchedule, SCHEDULE_ID)
        assert row is not None
        now = _utcnow()
        due, lease_active = _state(
            row, now=now, interval_minutes=interval_minutes
        )
        return PublicationScheduleSnapshot(
            status=row.status,
            due=due and not lease_active,
            lease_active=lease_active,
            last_started_at=_aware(row.last_started_at),
            last_completed_at=_aware(row.last_completed_at),
            last_published_count=row.last_published_count,
            last_error=row.last_error,
        )


async def claim_publication(
    sessions: async_sessionmaker[AsyncSession],
    *,
    force: bool,
    interval_minutes: int,
    lease_seconds: int,
) -> PublicationClaim | None:
    await ensure_schedule_row(sessions)
    now = _utcnow()
    async with sessions.begin() as session:
        row = await session.get(
            ApartmentPublicationSchedule, SCHEDULE_ID, with_for_update=True
        )
        assert row is not None
        due, lease_active = _state(
            row, now=now, interval_minutes=interval_minutes
        )
        if lease_active or (not force and not due):
            return None

        recovering = row.status in {"running", "failed"} and row.last_started_at is not None
        started_at = _aware(row.last_started_at) if recovering else now
        assert started_at is not None
        token = uuid.uuid4().hex
        row.status = "running"
        row.lease_token = token
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.last_started_at = started_at
        row.last_heartbeat_at = now
        row.last_error = None
        row.updated_at = now
        return PublicationClaim(token=token, started_at=started_at)


async def renew_publication_lease(
    sessions: async_sessionmaker[AsyncSession],
    *,
    token: str,
    lease_seconds: int,
) -> bool:
    now = _utcnow()
    async with sessions.begin() as session:
        row = await session.get(
            ApartmentPublicationSchedule, SCHEDULE_ID, with_for_update=True
        )
        if row is None or row.status != "running" or row.lease_token != token:
            return False
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.last_heartbeat_at = now
        row.updated_at = now
        return True


async def finish_publication(
    sessions: async_sessionmaker[AsyncSession],
    *,
    token: str,
    success: bool,
    published_count: int,
    error: str | None,
    retry_seconds: int = 60,
) -> bool:
    now = _utcnow()
    async with sessions.begin() as session:
        row = await session.get(
            ApartmentPublicationSchedule, SCHEDULE_ID, with_for_update=True
        )
        if row is None or row.lease_token != token:
            return False
        row.status = "succeeded" if success else "failed"
        row.lease_token = None
        row.lease_until = None if success else now + timedelta(seconds=max(30, retry_seconds))
        row.last_completed_at = now
        row.last_published_count = max(0, published_count)
        row.last_error = error
        row.updated_at = now
        return True


async def publication_heartbeat(
    sessions: async_sessionmaker[AsyncSession],
    *,
    token: str,
    lease_seconds: int,
    heartbeat_seconds: float,
) -> None:
    try:
        while True:
            await asyncio.sleep(max(15.0, heartbeat_seconds))
            if not await renew_publication_lease(
                sessions, token=token, lease_seconds=lease_seconds
            ):
                return
    except asyncio.CancelledError:
        raise


async def stop_heartbeat(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

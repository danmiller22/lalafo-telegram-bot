from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Apartment, ApartmentDiscoveryRun, ApartmentInventoryQueue


BISHKEK = ZoneInfo("Asia/Bishkek")
BATCH_SIZES = (4, 4, 4, 3, 3)
FIRST_HALF_CENTRAL = (3, 3, 2, 3, 2)  # 13; paired with 12 below = 25/day
SECOND_HALF_CENTRAL = (2, 3, 2, 3, 2)
MAX_TWO_BEDROOMS_PER_WINDOW = 2
MAX_TWO_BEDROOMS_PER_DAY = 20


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def discovery_period_start(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(timezone.utc)).astimezone(BISHKEK)
    hour = 0 if local.hour < 12 else 12
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def discovery_period_key(now: datetime | None = None) -> str:
    return discovery_period_start(now).strftime("%Y-%m-%dT%H:00+06")


def is_central(district: str | None) -> bool:
    # Importing this constant keeps discovery and queue planning on exactly the
    # same definition of "center".
    from scripts.scrape_publish import CENTRAL_DISTRICT_TERMS

    text = (district or "").casefold().replace("ё", "е")
    return any(term in text for term in CENTRAL_DISTRICT_TERMS)


@dataclass(frozen=True)
class PlannedApartment:
    apartment: Apartment
    scheduled_at: datetime
    window_key: str
    sequence: int


def _candidate_key(item: Apartment, *, central: bool) -> tuple[object, ...]:
    favorable = central and item.price <= 32_000
    return (
        not item.discovery_priority,
        not item.owner_listing,
        not favorable,
        -(as_utc(item.last_seen_at or item.updated_at).timestamp()),
        item.price,
        item.id,
    )


def _pick_for_window(
    pool: list[Apartment],
    count: int,
    room_counts: dict[str, int],
    *,
    two_bedroom_allowance: int,
    central: bool,
) -> list[Apartment]:
    picked: list[Apartment] = []
    two_count = 0
    while len(picked) < count:
        eligible = [
            item
            for item in pool
            if item.rooms != "2"
            or (two_count < MAX_TWO_BEDROOMS_PER_WINDOW and two_bedroom_allowance > 0)
        ]
        if not eligible:
            break
        eligible.sort(
            key=lambda item: (
                _candidate_key(item, central=central),
                room_counts.get(item.rooms, 0),
            )
        )
        item = eligible[0]
        pool.remove(item)
        picked.append(item)
        room_counts[item.rooms] = room_counts.get(item.rooms, 0) + 1
        if item.rooms == "2":
            two_count += 1
            two_bedroom_allowance -= 1
    return picked


def plan_period(
    apartments: list[Apartment],
    *,
    period_start: datetime,
    already_two_bedrooms_today: int = 0,
    rng: random.Random | None = None,
) -> list[PlannedApartment]:
    """Create five immutable random windows for one 12-hour period."""
    rng = rng or random.SystemRandom()
    central_pool = sorted(
        [item for item in apartments if is_central(item.district)],
        key=lambda item: _candidate_key(item, central=True),
    )
    other_pool = sorted(
        [item for item in apartments if not is_central(item.district)],
        key=lambda item: _candidate_key(item, central=False),
    )
    central_targets = (
        FIRST_HALF_CENTRAL if period_start.astimezone(BISHKEK).hour == 0 else SECOND_HALF_CENTRAL
    )
    # Fixed broad slots plus jitter give five random windows whose starts remain
    # at least 110 minutes apart. The exact card times are persisted in the DB.
    starts = [period_start + timedelta(minutes=25 + 135 * index + rng.randint(0, 25)) for index in range(5)]
    planned: list[PlannedApartment] = []
    room_counts: dict[str, int] = {}
    two_allowance = max(0, MAX_TWO_BEDROOMS_PER_DAY - already_two_bedrooms_today)

    for index, (batch_size, central_target, start) in enumerate(
        zip(BATCH_SIZES, central_targets, starts)
    ):
        if len(central_pool) < 2:
            break
        central_count = min(central_target, len(central_pool), batch_size)
        if central_count < 2:
            break
        selected = _pick_for_window(
            central_pool,
            central_count,
            room_counts,
            two_bedroom_allowance=two_allowance,
            central=True,
        )
        two_allowance -= sum(item.rooms == "2" for item in selected)
        remaining = batch_size - len(selected)
        others = _pick_for_window(
            other_pool,
            remaining,
            room_counts,
            two_bedroom_allowance=two_allowance,
            central=False,
        )
        two_allowance -= sum(item.rooms == "2" for item in others)
        selected.extend(others)
        # If non-central supply is short, add a central card, but never reduce
        # the required 2–3 central cards in the window.
        if len(selected) < batch_size and central_count < 3:
            extras = _pick_for_window(
                central_pool,
                min(3 - central_count, batch_size - len(selected)),
                room_counts,
                two_bedroom_allowance=two_allowance,
                central=True,
            )
            two_allowance -= sum(item.rooms == "2" for item in extras)
            selected.extend(extras)
        if not selected:
            continue
        rng.shuffle(selected)
        scheduled = start
        window_key = f"{period_start.strftime('%Y%m%dT%H')}-{index + 1}"
        for sequence, item in enumerate(selected, start=1):
            if sequence > 1:
                scheduled += timedelta(minutes=rng.randint(8, 15))
            planned.append(
                PlannedApartment(
                    apartment=item,
                    scheduled_at=scheduled.astimezone(timezone.utc),
                    window_key=window_key,
                    sequence=sequence,
                )
            )
    return planned


class InventoryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def claim_discovery(self, *, now: datetime, force: bool = False) -> str | None:
        key = discovery_period_key(now)
        lease_until = as_utc(now) + timedelta(hours=2)
        try:
            async with self.sessions.begin() as session:
                row = await session.get(ApartmentDiscoveryRun, key)
                if row is None:
                    session.add(
                        ApartmentDiscoveryRun(
                            period_key=key,
                            status="running",
                            lease_until=lease_until,
                            started_at=as_utc(now),
                        )
                    )
                    return key
                lease = as_utc(row.lease_until) if row.lease_until else None
                if not force and (row.status == "succeeded" or (row.status == "running" and lease and lease > as_utc(now))):
                    return None
                row.status = "running"
                row.lease_until = lease_until
                row.started_at = as_utc(now)
                row.completed_at = None
                row.last_error = None
                return key
        except IntegrityError:
            return None

    async def finish_discovery(
        self, key: str, *, success: bool, discovered: int, queued: int, error: str | None = None
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(ApartmentDiscoveryRun, key)
            if row is None:
                return
            row.status = "succeeded" if success else "failed"
            row.lease_until = None
            row.discovered_count = discovered
            row.queued_count = queued
            row.last_error = error
            row.completed_at = datetime.now(timezone.utc)

    async def period_counts(self, *, now: datetime | None = None) -> tuple[int, int]:
        now = as_utc(now or datetime.now(timezone.utc))
        prefix = discovery_period_start(now).strftime("%Y%m%dT%H") + "-%"
        async with self.sessions() as session:
            discovered = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(Apartment.last_seen_at >= now - timedelta(hours=2))
                )
                or 0
            )
            queued = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .where(ApartmentInventoryQueue.window_key.like(prefix))
                )
                or 0
            )
            return discovered, queued

    async def schedule_period(self, *, now: datetime | None = None, rng: random.Random | None = None) -> int:
        now = as_utc(now or datetime.now(timezone.utc))
        period = discovery_period_start(now)
        day_start_local = period.astimezone(BISHKEK).replace(hour=0)
        day_start = day_start_local.astimezone(timezone.utc)
        day_end = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)
        async with self.sessions.begin() as session:
            already_two = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.scheduled_at >= day_start,
                        ApartmentInventoryQueue.scheduled_at < day_end,
                        ApartmentInventoryQueue.status.in_(("queued", "publishing", "published")),
                        Apartment.rooms == "2",
                    )
                )
                or 0
            )
            queued_apartment_ids = select(ApartmentInventoryQueue.apartment_id)
            queued_fingerprints = select(Apartment.fingerprint).join(
                ApartmentInventoryQueue,
                ApartmentInventoryQueue.apartment_id == Apartment.id,
            )
            stock = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.active.is_(True),
                            Apartment.publication_status != "published",
                            Apartment.id.not_in(queued_apartment_ids),
                            Apartment.fingerprint.not_in(queued_fingerprints),
                            Apartment.price.between(20_000, 40_000),
                            Apartment.rooms.in_(("1", "2", "studio")),
                        )
                    )
                ).all()
            )
            planned = plan_period(
                stock,
                period_start=period,
                already_two_bedrooms_today=already_two,
                rng=rng,
            )
            if planned and planned[0].scheduled_at < now + timedelta(minutes=2):
                shift = now + timedelta(minutes=2) - planned[0].scheduled_at
                planned = [
                    PlannedApartment(
                        apartment=item.apartment,
                        scheduled_at=item.scheduled_at + shift,
                        window_key=item.window_key,
                        sequence=item.sequence,
                    )
                    for item in planned
                ]
            for item in planned:
                session.add(
                    ApartmentInventoryQueue(
                        apartment_id=item.apartment.id,
                        scheduled_at=item.scheduled_at,
                        window_key=item.window_key,
                        sequence=item.sequence,
                        status="queued",
                    )
                )
            return len(planned)

    async def claim_due(self, *, now: datetime | None = None) -> ApartmentInventoryQueue | None:
        now = as_utc(now or datetime.now(timezone.utc))
        stale = now - timedelta(minutes=20)
        async with self.sessions.begin() as session:
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(
                    ApartmentInventoryQueue.status == "publishing",
                    ApartmentInventoryQueue.claimed_at < stale,
                )
                .values(status="queued", claimed_at=None, last_error="stale_claim")
            )
            candidate = (
                select(ApartmentInventoryQueue.id)
                .where(
                    ApartmentInventoryQueue.status == "queued",
                    ApartmentInventoryQueue.scheduled_at <= now,
                )
                .order_by(ApartmentInventoryQueue.scheduled_at, ApartmentInventoryQueue.id)
                .limit(1)
                .scalar_subquery()
            )
            row = (
                await session.scalars(
                    update(ApartmentInventoryQueue)
                    .where(
                        ApartmentInventoryQueue.id == candidate,
                        ApartmentInventoryQueue.status == "queued",
                    )
                    .values(status="publishing", claimed_at=now, attempts=ApartmentInventoryQueue.attempts + 1)
                    .returning(ApartmentInventoryQueue)
                )
            ).first()
            if row is not None:
                await session.refresh(row, attribute_names=["apartment"])
            return row

    async def finish_item(self, queue_id: int, *, status: str, error: str | None = None) -> None:
        values: dict[str, object] = {
            "status": status,
            "last_error": error,
            "updated_at": datetime.now(timezone.utc),
        }
        if status == "published":
            values["published_at"] = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(ApartmentInventoryQueue.id == queue_id)
                .values(**values)
            )

    async def retry_item(self, queue_id: int, *, error: str, delay_minutes: int = 10) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(ApartmentInventoryQueue.id == queue_id)
                .values(
                    status="queued",
                    claimed_at=None,
                    scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
                    last_error=error,
                )
            )

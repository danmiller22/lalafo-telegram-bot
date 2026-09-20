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
FIRST_HALF_BATCH_SIZES = (6,) * 6
SECOND_HALF_BATCH_SIZES = (6,) * 6
# One persisted six-card window every two hours. Centre stock is preferred,
# but any suitable owner/realtor stock fills gaps so an empty centre pool does
# not stall publication.
FIRST_HALF_CENTRAL = FIRST_HALF_BATCH_SIZES
SECOND_HALF_CENTRAL = SECOND_HALF_BATCH_SIZES
MAX_TWO_BEDROOMS_PER_WINDOW = 1
MAX_TWO_BEDROOMS_PER_DAY = 10
MAX_TWO_BEDROOMS_PER_PERIOD = 5
MAX_PUBLICATIONS_PER_DAY = 72
MIN_REALTORS_PER_DAY = 6
MAX_REALTORS_PER_DAY = 8
TARGET_REALTORS_PER_PERIOD = 4
REPOST_AFTER_HOURS = 48
MAX_REPOSTS_PER_PERIOD = 36
DISCOVERY_RETRY_MINUTES = 30
MIN_HEALTHY_PERIOD_QUEUE = 30


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
        not favorable,
        item.price,
        not item.owner_listing,
        -(as_utc(item.last_seen_at or item.updated_at).timestamp()),
        item.id,
    )


def _seller_type(item: Apartment) -> str:
    value = getattr(item, "seller_type", "unknown")
    return value if value in {"owner", "realtor", "unknown"} else "unknown"


def _limit_realtors(
    planned: list[PlannedApartment],
    apartments: list[Apartment],
    *,
    target: int,
) -> list[PlannedApartment]:
    """Keep a period near its realtor quota without treating unknowns as agents."""
    target = max(0, target)
    result: list[PlannedApartment | None] = list(planned)
    used_ids = {item.apartment.id for item in planned}
    unused = [item for item in apartments if item.id not in used_ids]

    realtor_indexes = [
        index
        for index, item in enumerate(result)
        if item is not None and _seller_type(item.apartment) == "realtor"
    ]
    # Replace excess agents with an owner or an unspecified seller. Matching
    # the room count preserves the per-window two-bedroom ceiling.
    for index in realtor_indexes[target:]:
        current = result[index]
        if current is None:
            continue
        replacements = [
            item
            for item in unused
            if _seller_type(item) != "realtor"
            and item.rooms == current.apartment.rooms
        ]
        if replacements:
            replacements.sort(
                key=lambda item: (
                    is_central(item.district) != is_central(current.apartment.district),
                    _candidate_key(item, central=is_central(item.district)),
                )
            )
            replacement = replacements[0]
            unused.remove(replacement)
            result[index] = PlannedApartment(
                apartment=replacement,
                scheduled_at=current.scheduled_at,
                window_key=current.window_key,
                sequence=current.sequence,
            )
        else:
            # Never exceed the hard daily cap just to keep a window full.
            result[index] = None

    current_realtors = sum(
        item is not None and _seller_type(item.apartment) == "realtor"
        for item in result
    )
    if current_realtors < target:
        realtor_candidates = [item for item in unused if _seller_type(item) == "realtor"]
        realtor_candidates.sort(
            key=lambda item: _candidate_key(item, central=is_central(item.district))
        )
        for realtor in realtor_candidates:
            replaceable = [
                index
                for index, item in enumerate(result)
                if item is not None
                and _seller_type(item.apartment) != "realtor"
                and not item.apartment.discovery_priority
                and item.apartment.rooms == realtor.rooms
            ]
            if not replaceable:
                continue
            replaceable.sort(
                key=lambda index: (
                    is_central(result[index].apartment.district)
                    != is_central(realtor.district),
                    result[index].scheduled_at,
                )
            )
            index = replaceable[0]
            old = result[index]
            result[index] = PlannedApartment(
                apartment=realtor,
                scheduled_at=old.scheduled_at,
                window_key=old.window_key,
                sequence=old.sequence,
            )
            current_realtors += 1
            if current_realtors >= target:
                break

    return [item for item in result if item is not None]


def _pick_for_window(
    pool: list[Apartment],
    count: int,
    room_counts: dict[str, int],
    *,
    two_bedroom_allowance: int,
    central: bool,
    prefer_two: bool = False,
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
        def selection_key(item: Apartment) -> tuple[object, ...]:
            base = _candidate_key(item, central=central)
            if prefer_two and two_count == 0 and two_bedroom_allowance > 0:
                room_rank = {"2": 0, "1": 1, "studio": 2}.get(item.rooms, 3)
            else:
                # After the single two-bedroom slot, fill with one-bedroom
                # apartments first and use studios only as a fallback.
                room_rank = {"1": 0, "studio": 1, "2": 2}.get(item.rooms, 3)
            return (*base[:2], room_rank, room_counts.get(item.rooms, 0), *base[2:])

        eligible.sort(key=selection_key)
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
    realtor_target: int = TARGET_REALTORS_PER_PERIOD,
    repeat_apartment_ids: set[int] | None = None,
    rng: random.Random | None = None,
) -> list[PlannedApartment]:
    """Create one immutable six-card publication window every two hours."""
    rng = rng or random.SystemRandom()
    # The public catalogue is intentionally limited to ordinary one- and
    # two-bedroom apartments. Old studio rows may remain in the database but
    # must never return through the 48-hour repost fallback.
    apartments = [item for item in apartments if item.rooms in {"1", "2"}]
    repeat_ids = set(repeat_apartment_ids or ())
    central_pool = sorted(
        [
            item
            for item in apartments
            if is_central(item.district) and item.id not in repeat_ids
        ],
        key=lambda item: _candidate_key(item, central=True),
    )
    other_pool = sorted(
        [
            item
            for item in apartments
            if not is_central(item.district) and item.id not in repeat_ids
        ],
        key=lambda item: _candidate_key(item, central=False),
    )
    repeat_pool = [item for item in apartments if item.id in repeat_ids]
    rng.shuffle(repeat_pool)
    first_half = period_start.astimezone(BISHKEK).hour == 0
    batch_sizes = FIRST_HALF_BATCH_SIZES if first_half else SECOND_HALF_BATCH_SIZES
    repeat_windows = set(
        rng.sample(
            range(len(batch_sizes)),
            k=min(MAX_REPOSTS_PER_PERIOD, len(repeat_pool), len(batch_sizes)),
        )
    )
    central_targets = FIRST_HALF_CENTRAL if first_half else SECOND_HALF_CENTRAL
    # Every two hours gets a stable slot with a small persisted jitter. Six
    # cards remain 8-15 minutes apart and finish well inside their window.
    starts = [
        period_start + timedelta(minutes=5 + 120 * index + rng.randint(0, 4))
        for index in range(len(batch_sizes))
    ]
    planned: list[PlannedApartment] = []
    room_counts: dict[str, int] = {}
    two_allowance = min(
        MAX_TWO_BEDROOMS_PER_PERIOD,
        max(0, MAX_TWO_BEDROOMS_PER_DAY - already_two_bedrooms_today),
    )

    for index, (batch_size, central_target, start) in enumerate(
        zip(batch_sizes, central_targets, starts)
    ):
        selected: list[Apartment] = []
        window_two = 0
        if index in repeat_windows and repeat_pool:
            eligible_repeats = [
                item
                for item in repeat_pool
                if item.rooms != "2" or two_allowance > 0
            ]
            if eligible_repeats:
                central_repeats = [
                    item for item in eligible_repeats if is_central(item.district)
                ]
                repeat = rng.choice(central_repeats or eligible_repeats)
                repeat_pool.remove(repeat)
                selected.append(repeat)
                room_counts[repeat.rooms] = room_counts.get(repeat.rooms, 0) + 1
                if repeat.rooms == "2":
                    window_two = 1
                    two_allowance -= 1

        selected_central = sum(is_central(item.district) for item in selected)
        central_count = min(
            max(0, central_target - selected_central),
            len(central_pool),
            batch_size - len(selected),
        )
        selected.extend(
            _pick_for_window(
                central_pool,
                central_count,
                room_counts,
                two_bedroom_allowance=min(1 - window_two, two_allowance),
                central=True,
                prefer_two=window_two == 0,
            )
        )
        newly_selected_two = sum(item.rooms == "2" for item in selected) - window_two
        window_two += newly_selected_two
        two_allowance -= newly_selected_two
        remaining = batch_size - len(selected)
        others = _pick_for_window(
            other_pool,
            remaining,
            room_counts,
            two_bedroom_allowance=min(1 - window_two, two_allowance),
            central=False,
            prefer_two=window_two == 0,
        )
        other_two = sum(item.rooms == "2" for item in others)
        window_two += other_two
        two_allowance -= other_two
        selected.extend(others)
        # If non-central supply is short, fill the window from the central pool.
        # A centre shortage must never stop the publisher completely: after
        # taking every available central card, fill the remaining places from
        # the broader owner/realtor stock gathered by discovery.
        if len(selected) < batch_size:
            extras = _pick_for_window(
                central_pool,
                batch_size - len(selected),
                room_counts,
                two_bedroom_allowance=min(1 - window_two, two_allowance),
                central=True,
            )
            two_allowance -= sum(item.rooms == "2" for item in extras)
            selected.extend(extras)
        # Fresh cards have priority. If they run out, use randomly ordered
        # cards whose previous post is at least 48 hours old so no hourly slot
        # disappears merely because Lalafo has little new stock.
        while len(selected) < batch_size and repeat_pool:
            # Keep the random repost allocation spread across the remaining
            # hours. Only consume surplus repeats here; otherwise a thin fresh
            # pool would dump tomorrow's reserved repeats into this window.
            future_repeat_windows = sum(
                future_index in repeat_windows
                for future_index in range(index + 1, len(batch_sizes))
            )
            if len(repeat_pool) <= future_repeat_windows:
                break
            eligible_repeats = [
                item
                for item in repeat_pool
                if item.rooms != "2"
                or (window_two < MAX_TWO_BEDROOMS_PER_WINDOW and two_allowance > 0)
            ]
            if not eligible_repeats:
                break
            repeat = eligible_repeats[0]
            repeat_pool.remove(repeat)
            selected.append(repeat)
            room_counts[repeat.rooms] = room_counts.get(repeat.rooms, 0) + 1
            if repeat.rooms == "2":
                window_two += 1
                two_allowance -= 1
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
    return _limit_realtors(planned, apartments, target=realtor_target)


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
                    # A failed slot must be retried even when the wall clock
                    # has crossed into the next discovery period.
                    retry_cutoff = as_utc(now) - timedelta(minutes=DISCOVERY_RETRY_MINUTES)
                    failed = await session.scalar(
                        select(ApartmentDiscoveryRun)
                        .where(
                            ApartmentDiscoveryRun.status == "failed",
                            ApartmentDiscoveryRun.completed_at <= retry_cutoff,
                        )
                        .order_by(ApartmentDiscoveryRun.completed_at.asc())
                        .limit(1)
                    )
                    if failed is not None:
                        failed.status = "running"
                        failed.lease_until = lease_until
                        failed.started_at = as_utc(now)
                        failed.completed_at = None
                        failed.last_error = None
                        return failed.period_key
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
                if row.status == "running" and lease and lease > as_utc(now):
                    return None
                if (
                    not force
                    and row.status == "succeeded"
                    and row.queued_count >= MIN_HEALTHY_PERIOD_QUEUE
                ):
                    return None
                completed = as_utc(row.completed_at) if row.completed_at else None
                if (
                    not force
                    and row.status == "failed"
                    and completed
                    and completed > as_utc(now) - timedelta(minutes=DISCOVERY_RETRY_MINUTES)
                ):
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
            published_two = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                        Apartment.rooms == "2",
                    )
                )
                or 0
            )
            reserved_two = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.scheduled_at >= day_start,
                        ApartmentInventoryQueue.scheduled_at < day_end,
                        ApartmentInventoryQueue.status.in_(("queued", "publishing")),
                        Apartment.rooms == "2",
                    )
                )
                or 0
            )
            already_two = published_two + reserved_two
            published_realtors = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                        Apartment.seller_type == "realtor",
                    )
                )
                or 0
            )
            reserved_realtors = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.scheduled_at >= day_start,
                        ApartmentInventoryQueue.scheduled_at < day_end,
                        ApartmentInventoryQueue.status.in_(("queued", "publishing")),
                        Apartment.seller_type == "realtor",
                    )
                )
                or 0
            )
            already_realtors = published_realtors + reserved_realtors
            remaining_realtors = max(0, MAX_REALTORS_PER_DAY - already_realtors)
            if period.astimezone(BISHKEK).hour == 0:
                realtor_target = min(TARGET_REALTORS_PER_PERIOD, remaining_realtors)
            else:
                realtor_target = min(
                    remaining_realtors,
                    max(
                        TARGET_REALTORS_PER_PERIOD,
                        MIN_REALTORS_PER_DAY - already_realtors,
                    ),
                )
            active_queue_ids = select(ApartmentInventoryQueue.apartment_id).where(
                ApartmentInventoryQueue.status.in_(("queued", "publishing"))
            )
            queued_fingerprints = select(Apartment.fingerprint).join(
                ApartmentInventoryQueue,
                ApartmentInventoryQueue.apartment_id == Apartment.id,
            ).where(ApartmentInventoryQueue.status.in_(("queued", "publishing")))
            fresh_stock = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.active.is_(True),
                            Apartment.publication_status != "published",
                            Apartment.id.not_in(active_queue_ids),
                            Apartment.fingerprint.not_in(queued_fingerprints),
                            Apartment.price.between(20_000, 40_000),
                            Apartment.rooms.in_(("1", "2")),
                        )
                    )
                ).all()
            )
            repeat_stock = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.active.is_(True),
                            Apartment.publication_status == "published",
                            Apartment.published_at.is_not(None),
                            Apartment.published_at
                            <= now - timedelta(hours=REPOST_AFTER_HOURS),
                            Apartment.id.not_in(active_queue_ids),
                            Apartment.price.between(20_000, 40_000),
                            Apartment.rooms.in_(("1", "2")),
                        )
                    )
                ).all()
            )
            chooser = rng or random.SystemRandom()
            chosen_repeats = chooser.sample(
                repeat_stock,
                k=min(MAX_REPOSTS_PER_PERIOD, len(repeat_stock)),
            )
            stock = fresh_stock + chosen_repeats
            planned = plan_period(
                stock,
                period_start=period,
                already_two_bedrooms_today=already_two,
                realtor_target=realtor_target,
                repeat_apartment_ids={item.id for item in chosen_repeats},
                rng=chooser,
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
            planned_ids = [item.apartment.id for item in planned]
            existing_rows = {
                row.apartment_id: row
                for row in (
                    await session.scalars(
                        select(ApartmentInventoryQueue).where(
                            ApartmentInventoryQueue.apartment_id.in_(planned_ids)
                        )
                    )
                ).all()
            }
            for item in planned:
                existing = existing_rows.get(item.apartment.id)
                if existing is None:
                    session.add(
                        ApartmentInventoryQueue(
                            apartment_id=item.apartment.id,
                            scheduled_at=item.scheduled_at,
                            window_key=item.window_key,
                            sequence=item.sequence,
                            status="queued",
                        )
                    )
                    continue
                existing.scheduled_at = item.scheduled_at
                existing.window_key = item.window_key
                existing.sequence = item.sequence
                existing.status = "queued"
                existing.claimed_at = None
                existing.published_at = None
                existing.attempts = 0
                existing.last_error = None
            return len(planned)

    async def claim_due(
        self,
        *,
        now: datetime | None = None,
        eligible_until: datetime | None = None,
    ) -> ApartmentInventoryQueue | None:
        now = as_utc(now or datetime.now(timezone.utc))
        eligible_until = as_utc(eligible_until or now)
        stale = now - timedelta(minutes=20)
        local = now.astimezone(BISHKEK)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        day_end = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
        async with self.sessions.begin() as session:
            published_today = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                    )
                )
                or 0
            )
            if published_today >= MAX_PUBLICATIONS_PER_DAY:
                return None
            published_realtors = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                        Apartment.seller_type == "realtor",
                    )
                )
                or 0
            )
            publishing_realtors = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.status == "publishing",
                        ApartmentInventoryQueue.claimed_at >= day_start,
                        ApartmentInventoryQueue.claimed_at < day_end,
                        Apartment.seller_type == "realtor",
                    )
                )
                or 0
            )
            if published_realtors + publishing_realtors >= MAX_REALTORS_PER_DAY:
                excess_realtors = (
                    select(ApartmentInventoryQueue.id)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.status == "queued",
                        ApartmentInventoryQueue.scheduled_at <= eligible_until,
                        Apartment.seller_type == "realtor",
                    )
                )
                await session.execute(
                    update(ApartmentInventoryQueue)
                    .where(ApartmentInventoryQueue.id.in_(excess_realtors))
                    .values(status="skipped", last_error="daily_realtor_cap")
                )
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
                    ApartmentInventoryQueue.scheduled_at <= eligible_until,
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

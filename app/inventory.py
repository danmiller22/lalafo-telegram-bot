from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import random
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Apartment,
    ApartmentDiscoveryRun,
    ApartmentInventoryQueue,
    LalafoAutoReplyMeta,
)
from app.state import normalized_district
from app.telegram.formatting import is_supported_source


BISHKEK = ZoneInfo("Asia/Bishkek")
ALLOWED_ROOMS = frozenset({"studio", "1"})
CENTRAL_DAILY_SHARE = 0.50
MIN_PUBLICATIONS_PER_DAY = 50
MAX_PUBLICATIONS_PER_DAY = 60
# The daily target is chosen once per Bishkek date, then split across the two
# discovery periods so retries cannot increase the day's publication volume.
# Seller type does not restrict publication; these values remain for compatibility.
MIN_NON_OWNERS_PER_DAY = 0
MAX_NON_OWNERS_PER_DAY = MAX_PUBLICATIONS_PER_DAY
TARGET_NON_OWNERS_PER_PERIOD = 0
MAX_REPOSTS_PER_PERIOD = 0
MAX_FRESH_STOCK_LOAD = 600
# A blocked source must not leave the channel empty for half an hour.  The
# workflow itself is concurrency-limited, so a short retry is safe and lets
# Telegram reserve sources recover the queue on the next cloud tick.
DISCOVERY_RETRY_MINUTES = 3
MIN_HEALTHY_PERIOD_QUEUE = 20
PUBLICATION_SPACING_MINUTES = 3
# Five batches in each 12-hour discovery period. Together they cover the day
# from 05:00 through 23:00 Bishkek time at an exact two-hour cadence.
MORNING_BATCH_HOURS = (5, 7, 9, 11, 13)
EVENING_BATCH_HOURS = (15, 17, 19, 21, 23)


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


def daily_publication_target(period_start: datetime) -> int:
    """Return a stable pseudo-random 50-60 target for one Bishkek date."""
    local_date = period_start.astimezone(BISHKEK).date()
    seed = int(local_date.strftime("%Y%m%d"))
    return random.Random(seed).randint(
        MIN_PUBLICATIONS_PER_DAY, MAX_PUBLICATIONS_PER_DAY
    )


def period_publication_targets(period_start: datetime) -> tuple[int, int]:
    daily_total = daily_publication_target(period_start)
    first_count = (daily_total + 1) // 2
    first_central = round(first_count * CENTRAL_DAILY_SHARE)
    daily_central = round(daily_total * CENTRAL_DAILY_SHARE)
    if period_start.astimezone(BISHKEK).hour == 0:
        return first_count, first_central
    return daily_total - first_count, daily_central - first_central


def randomized_period_times(
    period_start: datetime,
    *,
    count: int,
    rng: random.Random,
) -> list[datetime]:
    """Build five two-hour batches with three minutes between cards.

    The daily total stays pseudo-random at 50-60 cards, while each individual
    batch contains five or six cards for normal production targets. ``rng``
    randomizes which batches receive the extra card without changing the
    durable cadence.
    """
    if count <= 0:
        return []
    local_day = period_start.astimezone(BISHKEK).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    batch_hours = (
        MORNING_BATCH_HOURS
        if period_start.astimezone(BISHKEK).hour == 0
        else EVENING_BATCH_HOURS
    )
    batch_sizes = [count // len(batch_hours)] * len(batch_hours)
    extra_batches = list(range(len(batch_hours)))
    rng.shuffle(extra_batches)
    for index in extra_batches[: count % len(batch_hours)]:
        batch_sizes[index] += 1

    result: list[datetime] = []
    for hour, batch_size in zip(batch_hours, batch_sizes):
        batch_start = local_day + timedelta(hours=hour)
        result.extend(
            (
                batch_start + timedelta(minutes=PUBLICATION_SPACING_MINUTES * offset)
            ).astimezone(timezone.utc)
            for offset in range(batch_size)
        )
    return result


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
    last_seen = as_utc(item.last_seen_at or item.updated_at)
    hot = last_seen >= datetime.now(timezone.utc) - timedelta(hours=24)
    return (
        0,
        not favorable,
        not hot,
        item.price,
        not item.discovery_priority,
        -last_seen.timestamp(),
        item.id,
    )


def _seller_type(item: Apartment) -> str:
    value = getattr(item, "seller_type", "unknown")
    return value if value in {"owner", "realtor", "unknown"} else "unknown"


def _supported_source_filter():
    return (
        Apartment.source_url.like("https://lalafo.kg/%")
        | Apartment.source_url.like("https://www.lalafo.kg/%")
        | Apartment.source_url.like("https://t.me/%")
        | Apartment.source_url.like("manual://telegram/%")
    )


def _limit_non_owners(
    planned: list[PlannedApartment],
    apartments: list[Apartment],
    *,
    target: int,
    maximum: int,
) -> list[PlannedApartment]:
    """Keep a tiny explicit-realtor slice without penalizing unknown authors."""
    maximum = max(0, maximum)
    target = min(maximum, max(0, target))
    result: list[PlannedApartment | None] = list(planned)
    used_ids = {item.apartment.id for item in planned}
    unused = [item for item in apartments if item.id not in used_ids]

    current_non_owners = sum(
        item is not None and _seller_type(item.apartment) == "realtor"
        for item in result
    )

    # Replace surplus realtor/unknown cards with verified owners. If verified
    # owner supply is thin, drop the surplus rather than misrepresenting it.
    if current_non_owners > maximum:
        owner_candidates = [item for item in unused if _seller_type(item) == "owner"]
        owner_candidates.sort(
            key=lambda item: _candidate_key(item, central=is_central(item.district))
        )
        surplus_indexes = [
            index
            for index, item in enumerate(result)
            if item is not None and _seller_type(item.apartment) == "realtor"
        ][maximum:]
        for index in surplus_indexes:
            old = result[index]
            replacement_index = next(
                (
                    candidate_index
                    for candidate_index, owner in enumerate(owner_candidates)
                    if owner.rooms == old.apartment.rooms
                    and is_central(owner.district) == is_central(old.apartment.district)
                ),
                None,
            )
            if replacement_index is None:
                replacement_index = next(
                    (
                        candidate_index
                        for candidate_index, owner in enumerate(owner_candidates)
                        if owner.rooms == old.apartment.rooms
                    ),
                    None,
                )
            if replacement_index is None:
                result[index] = None
            else:
                owner = owner_candidates.pop(replacement_index)
                result[index] = PlannedApartment(
                    apartment=owner,
                    scheduled_at=old.scheduled_at,
                    window_key=old.window_key,
                    sequence=old.sequence,
                )
        current_non_owners = sum(
            item is not None and _seller_type(item.apartment) == "realtor"
            for item in result
        )

    if current_non_owners < target:
        non_owner_candidates = [
            item for item in unused if _seller_type(item) == "realtor"
        ]
        non_owner_candidates.sort(
            key=lambda item: _candidate_key(item, central=is_central(item.district))
        )
        for candidate in non_owner_candidates:
            replaceable = [
                index
                for index, item in enumerate(result)
                if item is not None
                and _seller_type(item.apartment) != "realtor"
                and not item.apartment.discovery_priority
                and item.apartment.rooms == candidate.rooms
            ]
            if not replaceable:
                continue
            replaceable.sort(
                key=lambda index: (
                    is_central(result[index].apartment.district)
                    != is_central(candidate.district),
                    result[index].scheduled_at,
                )
            )
            index = replaceable[0]
            old = result[index]
            result[index] = PlannedApartment(
                apartment=candidate,
                scheduled_at=old.scheduled_at,
                window_key=old.window_key,
                sequence=old.sequence,
            )
            current_non_owners += 1
            if current_non_owners >= target:
                break

    return [item for item in result if item is not None]


def _pick_for_window(
    pool: list[Apartment],
    count: int,
    room_counts: dict[str, int],
    district_counts: dict[str, int],
    *,
    central: bool,
) -> list[Apartment]:
    picked: list[Apartment] = []
    while len(picked) < count:
        eligible = list(pool)
        if not eligible:
            break
        def selection_key(item: Apartment) -> tuple[object, ...]:
            base = _candidate_key(item, central=central)
            room_rank = {"1": 0, "studio": 1}.get(item.rooms, 2)
            district = normalized_district(item.district) or "unknown"
            return (
                district_counts.get(district, 0),
                room_counts.get(item.rooms, 0),
                room_rank,
                *base,
            )

        eligible.sort(key=selection_key)
        item = eligible[0]
        pool.remove(item)
        picked.append(item)
        room_counts[item.rooms] = room_counts.get(item.rooms, 0) + 1
        district = normalized_district(item.district) or "unknown"
        district_counts[district] = district_counts.get(district, 0) + 1
    return picked


def plan_period(
    apartments: list[Apartment],
    *,
    period_start: datetime,
    non_owner_target: int = TARGET_NON_OWNERS_PER_PERIOD,
    non_owner_limit: int = MAX_NON_OWNERS_PER_DAY,
    target_count_override: int | None = None,
    central_target_override: int | None = None,
    repeat_apartment_ids: set[int] | None = None,
    rng: random.Random | None = None,
) -> list[PlannedApartment]:
    """Create half of a random 50-60-card day in two-hour mini-batches."""
    rng = rng or random.SystemRandom()
    apartments = [
        item
        for item in apartments
        if item.rooms in ALLOWED_ROOMS
        and is_supported_source(item)
    ]
    repeat_ids = set(repeat_apartment_ids or ()) if MAX_REPOSTS_PER_PERIOD else set()
    central_pool = sorted(
        [
            item
            for item in apartments
            if is_central(item.district)
            and item.id not in repeat_ids
        ],
        key=lambda item: _candidate_key(item, central=True),
    )
    other_pool = sorted(
        [
            item
            for item in apartments
            if not is_central(item.district)
            and item.id not in repeat_ids
        ],
        key=lambda item: _candidate_key(item, central=False),
    )
    repeat_pool = [item for item in apartments if item.id in repeat_ids]
    target_count, central_target = period_publication_targets(period_start)
    if target_count_override is not None:
        target_count = max(0, target_count_override)
    if central_target_override is not None:
        central_target = max(0, min(target_count, central_target_override))
    room_counts: dict[str, int] = {}
    district_counts: dict[str, int] = {}
    selected = _pick_for_window(
        central_pool,
        min(central_target, len(central_pool)),
        room_counts,
        district_counts,
        central=True,
    )
    selected.extend(
        _pick_for_window(
            other_pool,
            target_count - len(selected),
            room_counts,
            district_counts,
            central=False,
        )
    )
    if len(selected) < target_count:
        selected.extend(
            _pick_for_window(
                central_pool,
                target_count - len(selected),
                room_counts,
                district_counts,
                central=True,
            )
        )
    if len(selected) < target_count and repeat_pool:
        rng.shuffle(repeat_pool)
        selected.extend(repeat_pool[: target_count - len(selected)])
    rng.shuffle(selected)
    schedule = randomized_period_times(
        period_start,
        count=len(selected),
        rng=rng,
    )
    window_key = f"{period_start.strftime('%Y%m%dT%H')}-two-hour-batches"
    planned = [
        PlannedApartment(
            apartment=item,
            scheduled_at=scheduled,
            window_key=window_key,
            sequence=sequence,
        )
        for sequence, (item, scheduled) in enumerate(
            zip(selected, schedule), start=1
        )
    ]
    return planned


class InventoryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def reset_publication_history_for_code(self, code_version: str) -> bool:
        """Start a fresh publication epoch once for each deployed code version.

        Historical Telegram messages and payment records stay intact. Only the
        deduplication status and unpublished queue are reset, which lets the
        same source apartments be selected again after a code update.
        """
        version = code_version.strip()
        if not version:
            return False
        key = "apartment_publication_code_version"
        async with self.sessions.begin() as session:
            marker = await session.get(LalafoAutoReplyMeta, key)
            if marker is not None and marker.value == version:
                return False
            await session.execute(delete(ApartmentInventoryQueue))
            await session.execute(delete(ApartmentDiscoveryRun))
            await session.execute(
                update(Apartment)
                .where(Apartment.active.is_(True))
                .values(publication_status="discovered")
            )
            if marker is None:
                session.add(LalafoAutoReplyMeta(key=key, value=version))
            else:
                marker.value = version
        return True

    async def claim_availability_sweep(
        self, *, now: datetime, interval_hours: int = 12
    ) -> bool:
        key = "apartment_availability_last_sweep"
        current = as_utc(now)
        async with self.sessions.begin() as session:
            marker = await session.get(LalafoAutoReplyMeta, key)
            if marker is not None:
                try:
                    last_run = as_utc(datetime.fromisoformat(marker.value))
                except ValueError:
                    last_run = current - timedelta(days=1)
                if current - last_run < timedelta(hours=max(1, interval_hours)):
                    return False
                marker.value = current.isoformat()
            else:
                session.add(LalafoAutoReplyMeta(key=key, value=current.isoformat()))
        return True

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
                if (
                    not force
                    and row.status == "running"
                    and lease
                    and lease > as_utc(now)
                ):
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
                    .where(
                        ApartmentInventoryQueue.window_key.like(prefix),
                        ApartmentInventoryQueue.status == "queued",
                    )
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
            invalid_room_ids = select(Apartment.id).where(
                Apartment.rooms.not_in(ALLOWED_ROOMS)
            )
            await session.execute(
                delete(ApartmentInventoryQueue).where(
                    ApartmentInventoryQueue.apartment_id.in_(invalid_room_ids)
                )
            )
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
            reserved_today = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .where(
                        ApartmentInventoryQueue.scheduled_at >= day_start,
                        ApartmentInventoryQueue.scheduled_at < day_end,
                        ApartmentInventoryQueue.status.in_(("queued", "publishing")),
                    )
                )
                or 0
            )
            published_central = sum(
                is_central(district)
                for district in (
                    await session.scalars(
                        select(Apartment.district).where(
                            Apartment.publication_status == "published",
                            Apartment.published_at >= day_start,
                            Apartment.published_at < day_end,
                        )
                    )
                ).all()
            )
            reserved_central = sum(
                is_central(district)
                for district in (
                    await session.scalars(
                        select(Apartment.district)
                        .join(ApartmentInventoryQueue)
                        .where(
                            ApartmentInventoryQueue.scheduled_at >= day_start,
                            ApartmentInventoryQueue.scheduled_at < day_end,
                            ApartmentInventoryQueue.status.in_(
                                ("queued", "publishing")
                            ),
                        )
                    )
                ).all()
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
                            Apartment.publication_status == "discovered",
                            _supported_source_filter(),
                            Apartment.id.not_in(active_queue_ids),
                            Apartment.fingerprint.not_in(queued_fingerprints),
                            Apartment.price.between(18_000, 40_000),
                            Apartment.rooms.in_(ALLOWED_ROOMS),
                            Apartment.last_seen_at >= now - timedelta(days=2),
                        )
                        .order_by(
                            Apartment.discovery_priority.desc(),
                            Apartment.last_seen_at.desc(),
                            Apartment.price.asc(),
                        )
                        .limit(MAX_FRESH_STOCK_LOAD)
                    )
                ).all()
            )
            chooser = rng or random.SystemRandom()
            target_override = None
            central_override = None
            if period.astimezone(BISHKEK).hour != 0:
                daily_target = daily_publication_target(period)
                target_override = max(
                    0, daily_target - published_today - reserved_today
                )
                daily_central_target = round(daily_target * CENTRAL_DAILY_SHARE)
                central_override = max(
                    0,
                    daily_central_target - published_central - reserved_central,
                )
            planned = plan_period(
                fresh_stock,
                period_start=period,
                target_count_override=target_override,
                central_target_override=central_override,
                rng=chooser,
            )
            if planned and planned[0].scheduled_at < now + timedelta(minutes=2):
                shift = now - timedelta(seconds=1) - planned[0].scheduled_at
                planned = [
                    PlannedApartment(
                        apartment=item.apartment,
                        scheduled_at=item.scheduled_at + shift,
                        window_key=item.window_key,
                        sequence=item.sequence,
                    )
                    for item in planned
                ]
            if planned:
                local_now = now.astimezone(BISHKEK)
                day_end = (
                    local_now.replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                ).astimezone(timezone.utc)
                if planned[-1].scheduled_at >= day_end:
                    start = now - timedelta(seconds=1)
                    usable_seconds = max(1.0, (day_end - start).total_seconds())
                    slot_seconds = usable_seconds / (len(planned) + 1)
                    planned = [
                        PlannedApartment(
                            apartment=item.apartment,
                            scheduled_at=(
                                start
                                if index == 0
                                else start
                                + timedelta(
                                    seconds=slot_seconds
                                    * (index + chooser.uniform(0.15, 0.85))
                                )
                            ),
                            window_key=item.window_key,
                            sequence=item.sequence,
                        )
                        for index, item in enumerate(planned)
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
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(
                    ApartmentInventoryQueue.status == "publishing",
                    ApartmentInventoryQueue.claimed_at < stale,
                )
                .values(status="queued", claimed_at=None, last_error="stale_claim")
            )
            unconfirmed_apartment_ids = select(Apartment.id).where(~_supported_source_filter())
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(
                    ApartmentInventoryQueue.status == "queued",
                    ApartmentInventoryQueue.apartment_id.in_(unconfirmed_apartment_ids),
                )
                .values(status="skipped", last_error="unsupported_source")
            )
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
            published_non_owners = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                        func.coalesce(Apartment.seller_type, "unknown") == "realtor",
                    )
                )
                or 0
            )
            publishing_non_owners = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentInventoryQueue)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.status == "publishing",
                        ApartmentInventoryQueue.claimed_at >= day_start,
                        ApartmentInventoryQueue.claimed_at < day_end,
                        func.coalesce(Apartment.seller_type, "unknown") == "realtor",
                    )
                )
                or 0
            )
            non_owner_cap_reached = False
            if non_owner_cap_reached:
                excess_non_owners = (
                    select(ApartmentInventoryQueue.id)
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.status == "queued",
                        ApartmentInventoryQueue.scheduled_at <= eligible_until,
                        func.coalesce(Apartment.seller_type, "unknown") == "realtor",
                    )
                )
                await session.execute(
                    update(ApartmentInventoryQueue)
                    .where(ApartmentInventoryQueue.id.in_(excess_non_owners))
                    .values(status="skipped", last_error="daily_non_owner_cap")
                )
            published_districts = (
                await session.scalars(
                    select(Apartment.district).where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= day_start,
                        Apartment.published_at < day_end,
                    )
                )
            ).all()
            published_central = sum(is_central(value) for value in published_districts)
            due_rows = (
                await session.execute(
                    select(
                        ApartmentInventoryQueue.id,
                        Apartment.district,
                    )
                    .join(Apartment)
                    .where(
                        ApartmentInventoryQueue.status == "queued",
                        ApartmentInventoryQueue.scheduled_at <= eligible_until,
                        Apartment.rooms.in_(ALLOWED_ROOMS),
                        _supported_source_filter(),
                    )
                    .order_by(
                        ApartmentInventoryQueue.scheduled_at,
                        ApartmentInventoryQueue.id,
                    )
                    .limit(200)
                )
            ).all()
            if not due_rows:
                return None
            central_needed = published_central < math.ceil(
                (published_today + 1) * CENTRAL_DAILY_SHARE
            )
            selected_id = next(
                (
                    queue_id
                    for queue_id, district in due_rows
                    if central_needed and is_central(district)
                ),
                due_rows[0][0],
            )
            row = (
                await session.scalars(
                    update(ApartmentInventoryQueue)
                    .where(
                        ApartmentInventoryQueue.id == selected_id,
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

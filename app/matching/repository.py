from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import (
    Apartment,
    ApartmentMatchDelivery,
    ApartmentSearchProfile,
    CustomerFunnelEvent,
)
from app.state import normalized_district


FRESH_FOR = timedelta(days=2)
MIN_PRICE = 23_000
MAX_PRICE = 40_000
COMMON_DISTRICTS = (
    "Центр", "ЦУМ", "ГУМ", "Золотой квадрат", "Филармония", "Ошский рынок",
    "Восток-5", "Асанбай", "Джал", "Верхний Джал", "Тунгуч", "Юг-2",
    "Аламедин-1", "Моссовет", "Медакадемия", "Политех", "Токольдош",
    "Ботанический сад", "Орто-Сайский рынок", "Парк Ататюрка", "Южная магистраль",
    "3 мкр", "4 мкр", "5 мкр", "6 мкр", "7 мкр", "8 мкр", "9 мкр",
    "10 мкр", "11 мкр", "12 мкр",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def apartment_matches(apartment: Apartment, profile: ApartmentSearchProfile) -> bool:
    if apartment.rooms not in set(profile.rooms or []):
        return False
    if apartment.price < MIN_PRICE or apartment.price > profile.max_budget:
        return False
    if profile.all_districts:
        return True
    apartment_district = normalized_district(apartment.district)
    return bool(apartment_district) and apartment_district in {
        normalized_district(value) for value in profile.districts or []
    }


@dataclass(frozen=True)
class ClaimedDelivery:
    delivery_id: int
    user_id: int
    apartment: Apartment


class MatchingRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        bind = sessions.kw.get("bind")
        self.dialect = bind.dialect.name if bind is not None else ""

    async def profile(self, user_id: int) -> ApartmentSearchProfile | None:
        async with self.sessions() as session:
            return await session.get(ApartmentSearchProfile, user_id)

    async def save_profile(
        self,
        *,
        user_id: int,
        rooms: list[str],
        districts: list[str],
        all_districts: bool,
        max_budget: int,
        source: str,
    ) -> ApartmentSearchProfile:
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            profile = await session.get(ApartmentSearchProfile, user_id)
            if profile is None:
                profile = ApartmentSearchProfile(
                    telegram_user_id=user_id,
                    rooms=rooms,
                    districts=[] if all_districts else districts,
                    all_districts=all_districts,
                    max_budget=max_budget,
                    notifications_enabled=True,
                    source=source,
                    notifications_after=now,
                )
                session.add(profile)
            else:
                profile.rooms = rooms
                profile.districts = [] if all_districts else districts
                profile.all_districts = all_districts
                profile.max_budget = max_budget
                profile.notifications_enabled = True
                profile.source = source or profile.source
                profile.notifications_after = now
                profile.updated_at = now
            await session.flush()
            await session.refresh(profile)
            return profile

    async def toggle_notifications(self, user_id: int) -> bool | None:
        async with self.sessions.begin() as session:
            profile = await session.get(ApartmentSearchProfile, user_id)
            if profile is None:
                return None
            profile.notifications_enabled = not profile.notifications_enabled
            if profile.notifications_enabled:
                profile.notifications_after = datetime.now(timezone.utc)
            return profile.notifications_enabled

    async def available_districts(self) -> list[str]:
        cutoff = datetime.now(timezone.utc) - FRESH_FOR
        async with self.sessions() as session:
            values = list(
                (
                    await session.scalars(
                        select(Apartment.district)
                        .where(
                            Apartment.active.is_(True),
                            Apartment.published_at.is_not(None),
                            Apartment.published_at >= cutoff,
                            Apartment.rooms.in_(("studio", "1")),
                            Apartment.price.between(MIN_PRICE, MAX_PRICE),
                            Apartment.district.is_not(None),
                        )
                        .distinct()
                    )
                ).all()
            )
        clean: dict[str, str] = {}
        for value in (*COMMON_DISTRICTS, *values):
            if not value or not value.strip():
                continue
            label = " ".join(value.split())
            clean.setdefault(normalized_district(label), label)
        return sorted(clean.values(), key=lambda value: value.casefold())

    async def _fresh_apartments(self, *, limit: int = 500) -> list[Apartment]:
        cutoff = datetime.now(timezone.utc) - FRESH_FOR
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Apartment)
                        .where(
                            Apartment.active.is_(True),
                            Apartment.publication_status == "published",
                            Apartment.published_at.is_not(None),
                            Apartment.published_at >= cutoff,
                            Apartment.rooms.in_(("studio", "1")),
                            Apartment.price.between(MIN_PRICE, MAX_PRICE),
                            Apartment.phone != "",
                            Apartment.availability_status != "unavailable",
                        )
                        .order_by(
                            Apartment.published_at.desc(),
                            Apartment.discovery_priority.desc(),
                            Apartment.id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )

    async def hot_apartments(
        self, limit: int = 2, *, prefer_lalafo: bool = False
    ) -> list[Apartment]:
        rows = [item for item in await self._fresh_apartments() if item.photo_urls]
        if prefer_lalafo:
            # Lalafo visitors arrive with the intent to ask about an apartment.
            # Keep that intent by putting fresh Lalafo cards first, while still
            # falling back to the full inventory when Lalafo has fewer cards.
            rows.sort(
                key=lambda item: "lalafo.kg" not in (item.source_url or "").casefold()
            )
        selected: list[Apartment] = []
        seen_districts: set[str] = set()
        for item in rows:
            district = normalized_district(item.district)
            if district and district in seen_districts:
                continue
            selected.append(item)
            seen_districts.add(district)
            if len(selected) == limit:
                return selected
        selected_ids = {item.id for item in selected}
        selected.extend(item for item in rows if item.id not in selected_ids)
        return selected[:limit]

    async def matching_apartments(
        self,
        profile: ApartmentSearchProfile,
        *,
        limit: int = 3,
        exclude_ids: set[int] | None = None,
        exclude_delivered: bool = True,
    ) -> list[Apartment]:
        excluded = set(exclude_ids or set())
        if exclude_delivered:
            async with self.sessions() as session:
                excluded.update(
                    (
                        await session.scalars(
                            select(ApartmentMatchDelivery.apartment_id).where(
                                ApartmentMatchDelivery.telegram_user_id
                                == profile.telegram_user_id,
                                ApartmentMatchDelivery.status == "delivered",
                            )
                        )
                    ).all()
                )
        return [
            item
            for item in await self._fresh_apartments()
            if item.id not in excluded and item.photo_urls and apartment_matches(item, profile)
        ][:limit]

    async def record_shown(
        self, user_id: int, apartments: list[Apartment], *, kind: str
    ) -> None:
        if not apartments:
            return
        ids = {item.id for item in apartments}
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            existing = set(
                (
                    await session.scalars(
                        select(ApartmentMatchDelivery.apartment_id).where(
                            ApartmentMatchDelivery.telegram_user_id == user_id,
                            ApartmentMatchDelivery.apartment_id.in_(ids),
                        )
                    )
                ).all()
            )
            session.add_all(
                ApartmentMatchDelivery(
                    telegram_user_id=user_id,
                    apartment_id=apartment.id,
                    kind=kind,
                    status="delivered",
                    attempts=1,
                    updated_at=now,
                )
                for apartment in apartments
                if apartment.id not in existing
            )

    async def event(
        self,
        user_id: int,
        event_name: str,
        *,
        source: str = "telegram",
        apartment_id: int | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            session.add(
                CustomerFunnelEvent(
                    telegram_user_id=user_id,
                    event_name=event_name,
                    source=source,
                    apartment_id=apartment_id,
                )
            )

    async def stats(self, *, days: int = 7) -> dict[str, int]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        async with self.sessions() as session:
            result = await session.execute(
                select(
                    CustomerFunnelEvent.source,
                    CustomerFunnelEvent.event_name,
                    func.count(),
                )
                .where(CustomerFunnelEvent.created_at >= cutoff)
                .group_by(
                    CustomerFunnelEvent.source, CustomerFunnelEvent.event_name
                )
            )
            counts: dict[str, int] = {}
            for source, name, count in result:
                counts[str(name)] = counts.get(str(name), 0) + int(count)
                counts[f"{source}:{name}"] = int(count)
            counts["profiles"] = int(
                await session.scalar(
                    select(func.count()).select_from(ApartmentSearchProfile)
                )
                or 0
            )
            counts["notifications"] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ApartmentMatchDelivery)
                    .where(
                        ApartmentMatchDelivery.kind == "notification",
                        ApartmentMatchDelivery.status == "delivered",
                    )
                )
                or 0
            )
            return counts

    async def seed_notifications(self) -> int:
        apartments = await self._fresh_apartments()
        if not apartments:
            return 0
        created = 0
        async with self.sessions.begin() as session:
            profiles = list(
                (
                    await session.scalars(
                        select(ApartmentSearchProfile).where(
                            ApartmentSearchProfile.notifications_enabled.is_(True)
                        )
                    )
                ).all()
            )
            if not profiles:
                return 0
            user_ids = [item.telegram_user_id for item in profiles]
            apartment_ids = [item.id for item in apartments]
            existing = set(
                (
                    await session.execute(
                        select(
                            ApartmentMatchDelivery.telegram_user_id,
                            ApartmentMatchDelivery.apartment_id,
                        ).where(
                            ApartmentMatchDelivery.telegram_user_id.in_(user_ids),
                            ApartmentMatchDelivery.apartment_id.in_(apartment_ids),
                        )
                    )
                ).all()
            )
            now = datetime.now(timezone.utc)
            for profile in profiles:
                after = _aware(profile.notifications_after) or now
                for apartment in apartments:
                    published_at = _aware(apartment.published_at)
                    pair = (profile.telegram_user_id, apartment.id)
                    if (
                        pair in existing
                        or published_at is None
                        or published_at < after
                        or not apartment_matches(apartment, profile)
                    ):
                        continue
                    values = {
                        "telegram_user_id": profile.telegram_user_id,
                        "apartment_id": apartment.id,
                        "kind": "notification",
                        "status": "queued",
                        "next_attempt_at": now,
                        "attempts": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                    if self.dialect == "postgresql":
                        statement = postgresql_insert(ApartmentMatchDelivery).values(**values)
                        statement = statement.on_conflict_do_nothing(
                            constraint="uq_match_user_apartment"
                        )
                        result = await session.execute(statement)
                        if result.rowcount != 1:
                            continue
                    elif self.dialect == "sqlite":
                        statement = sqlite_insert(ApartmentMatchDelivery).values(**values)
                        statement = statement.on_conflict_do_nothing(
                            index_elements=["telegram_user_id", "apartment_id"]
                        )
                        result = await session.execute(statement)
                        if result.rowcount != 1:
                            continue
                    else:
                        session.add(ApartmentMatchDelivery(**values))
                    existing.add(pair)
                    created += 1
        return created

    async def claim_delivery(self) -> ClaimedDelivery | None:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=5)
        async with self.sessions.begin() as session:
            delivery = await session.scalar(
                select(ApartmentMatchDelivery)
                .options(selectinload(ApartmentMatchDelivery.apartment))
                .where(
                    ApartmentMatchDelivery.attempts < 5,
                    or_(
                        and_(
                            ApartmentMatchDelivery.status.in_(("queued", "retry")),
                            or_(
                                ApartmentMatchDelivery.next_attempt_at.is_(None),
                                ApartmentMatchDelivery.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            ApartmentMatchDelivery.status == "sending",
                            ApartmentMatchDelivery.updated_at <= stale,
                        ),
                    ),
                )
                .order_by(ApartmentMatchDelivery.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if delivery is None:
                return None
            profile = await session.get(
                ApartmentSearchProfile, delivery.telegram_user_id
            )
            if profile is None or not profile.notifications_enabled:
                delivery.status = "cancelled"
                delivery.updated_at = now
                return None
            delivery.status = "sending"
            delivery.attempts += 1
            delivery.updated_at = now
            apartment = delivery.apartment
            return ClaimedDelivery(delivery.id, delivery.telegram_user_id, apartment)

    async def finish_delivery(
        self,
        delivery_id: int,
        *,
        message_id: int | None = None,
        error: str | None = None,
        retry_after: float = 60,
        disable_profile: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            delivery = await session.get(ApartmentMatchDelivery, delivery_id)
            if delivery is None:
                return
            if error is None:
                delivery.status = "delivered"
                delivery.telegram_message_id = message_id
                delivery.next_attempt_at = None
                delivery.last_error = None
            else:
                delivery.status = "failed" if delivery.attempts >= 5 else "retry"
                delivery.next_attempt_at = now + timedelta(seconds=max(5, retry_after))
                delivery.last_error = error[:100]
            delivery.updated_at = now
            if disable_profile:
                profile = await session.get(
                    ApartmentSearchProfile, delivery.telegram_user_id
                )
                if profile is not None:
                    profile.notifications_enabled = False

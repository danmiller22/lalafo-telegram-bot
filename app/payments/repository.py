from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.listing_lifecycle import listing_expired
from app.models import Apartment, ApartmentInventoryQueue, DailyFeaturedPublication, PaymentHistory, PaymentRequest
from app.payment_plans import LIFETIME_PLAN, MONTH_PLAN, WEEK_PLAN, expires_at_for, plan_price
from app.state import ListingIndex, ad_fingerprint
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION


@dataclass(frozen=True)
class PaymentSubmission:
    request: PaymentRequest
    outcome: str


@dataclass(frozen=True)
class ManagedLalafoSource:
    """Original contact card paired with the ad shown on our Lalafo profile."""

    apartment: Apartment
    managed_lalafo_ad_id: int | None
    managed_lalafo_ad_url: str | None


class ApartmentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get(self, apartment_id: int) -> Apartment | None:
        async with self.sessions() as session:
            return await session.get(Apartment, apartment_id)

    async def get_by_lalafo(self, lalafo_id: int) -> Apartment | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(Apartment).where(Apartment.lalafo_id == lalafo_id)
            )
            return result.scalar_one_or_none()

    async def published_author_count(
        self,
        seller_type: str,
        *,
        since: datetime,
        until: datetime,
    ) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Apartment)
                    .where(
                        Apartment.publication_status == "published",
                        Apartment.published_at >= since,
                        Apartment.published_at < until,
                        Apartment.seller_type == seller_type,
                    )
                )
                or 0
            )

    async def availability_candidates(
        self, *, published_since: datetime, limit: int = 500
    ) -> list[int]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Apartment.id)
                        .where(
                            Apartment.active.is_(True),
                            Apartment.telegram_message_id.is_not(None),
                            Apartment.published_at.is_not(None),
                            Apartment.published_at >= published_since,
                        )
                        .order_by(
                            Apartment.availability_checked_at.asc().nullsfirst(),
                            Apartment.published_at.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )

    async def expire_published_before(
        self, *, cutoff: datetime, checked_at: datetime
    ) -> int:
        async with self.sessions.begin() as session:
            ids = list(
                (
                    await session.scalars(
                        select(Apartment.id).where(
                            Apartment.active.is_(True),
                            Apartment.telegram_message_id.is_not(None),
                            Apartment.published_at.is_not(None),
                            Apartment.published_at < cutoff,
                            Apartment.last_seen_at < cutoff,
                        )
                    )
                ).all()
            )
            if not ids:
                return 0
            result = await session.execute(
                update(Apartment)
                .where(Apartment.id.in_(ids))
                .values(
                    active=False,
                    availability_status="unavailable",
                    availability_checked_at=checked_at,
                    availability_reason="listing_age_limit",
                )
            )
            await session.execute(
                update(ApartmentInventoryQueue)
                .where(
                    ApartmentInventoryQueue.apartment_id.in_(ids),
                    ApartmentInventoryQueue.status.in_(("queued", "publishing")),
                )
                .values(status="skipped", last_error="listing_age_limit")
            )
            return int(result.rowcount or 0)

    async def get_by_telegram_message(
        self, *, chat_id: int, message_id: int
    ) -> Apartment | None:
        async with self.sessions() as session:
            apartment = await session.scalar(
                select(Apartment).where(
                    Apartment.telegram_chat_id == chat_id,
                    Apartment.telegram_message_id == message_id,
                )
            )
            if apartment is not None:
                return apartment
            from app.models import RequestedCardPublication
            return await session.scalar(select(Apartment).join(
                RequestedCardPublication, RequestedCardPublication.apartment_id == Apartment.id,
            ).where(RequestedCardPublication.telegram_chat_id == chat_id,
                    RequestedCardPublication.telegram_message_id == message_id))

    async def find_forwarded_card(
        self,
        *,
        rooms: str,
        district: str,
        price: int,
        origin_date: datetime | None = None,
    ) -> Apartment | None:
        """Resolve a forwarded public card without trusting its lost buttons."""
        normalized_district = " ".join(district.split()).casefold().replace("ё", "е")
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Apartment)
                        .where(
                            Apartment.rooms == rooms,
                            Apartment.price == price,
                            Apartment.telegram_message_id.is_not(None),
                        )
                        .order_by(Apartment.published_at.desc(), Apartment.id.desc())
                        .limit(100)
                    )
                ).all()
            )
        matches = [
            item
            for item in rows
            if " ".join((item.district or "").split()).casefold().replace("ё", "е")
            == normalized_district
        ]
        if not matches:
            return None
        if origin_date is None:
            return matches[0]
        if origin_date.tzinfo is None:
            origin_date = origin_date.replace(tzinfo=timezone.utc)

        def distance(item: Apartment) -> float:
            published = item.published_at or item.updated_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            return abs((published.astimezone(timezone.utc) - origin_date).total_seconds())

        return min(matches, key=distance)

    async def curated_rotation_apartments(
        self, specs: tuple[tuple[str, int], ...]
    ) -> list[Apartment]:
        """Resolve one verified, photo-backed apartment for every curated spec.

        Curated cards reuse the original source apartment stored by the payment
        bot.  That preserves the private owner phone for paid delivery; the
        public Lalafo repost deliberately has no phone and is never used as the
        contact source.
        """
        if not specs:
            return []
        prices = {price for _, price in specs}
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Apartment)
                        .where(
                            Apartment.price.in_(prices),
                            Apartment.rooms == "1",
                            Apartment.active.is_(True),
                            Apartment.phone != "",
                        )
                        .order_by(
                            Apartment.source_updated_at.desc(),
                            Apartment.updated_at.desc(),
                            Apartment.id.desc(),
                        )
                    )
                ).all()
            )
        selected: list[Apartment] = []
        used_ids: set[int] = set()
        for district, price in specs:
            normalized = district.casefold().replace("ё", "е")
            match = next(
                (
                    apartment
                    for apartment in rows
                    if apartment.id not in used_ids
                    and apartment.price == price
                    and apartment.photo_urls
                    and normalized
                    in (apartment.district or "").casefold().replace("ё", "е")
                ),
                None,
            )
            if match is not None:
                selected.append(match)
                used_ids.add(match.id)
        return selected

    async def curated_rotation_apartments_by_ids(
        self, lalafo_ids: tuple[int, ...]
    ) -> list[Apartment]:
        """Resolve explicitly approved source advertisements for rotation."""
        if not lalafo_ids:
            return []
        positions = {lalafo_id: index for index, lalafo_id in enumerate(lalafo_ids)}
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.lalafo_id.in_(lalafo_ids),
                            Apartment.rooms == "1",
                            Apartment.active.is_(True),
                            Apartment.phone != "",
                        )
                    )
                ).all()
            )
        return sorted(rows, key=lambda apartment: positions[apartment.lalafo_id])

    async def managed_lalafo_source_apartments(self) -> list[Apartment]:
        """Resolve original source cards behind ads created on our Lalafo profile."""
        return [item.apartment for item in await self.managed_lalafo_sources()]

    async def managed_lalafo_sources(self) -> list[ManagedLalafoSource]:
        """Return active managed ads together with their original source cards."""
        async with self.sessions() as session:
            references = (
                await session.execute(
                    select(
                        DailyFeaturedPublication.source_apartment_id,
                        DailyFeaturedPublication.source_lalafo_id,
                        DailyFeaturedPublication.managed_lalafo_ad_id,
                        DailyFeaturedPublication.managed_lalafo_ad_url,
                    )
                    .where(
                        or_(
                            DailyFeaturedPublication.managed_lalafo_ad_id.is_not(None),
                            DailyFeaturedPublication.managed_lalafo_ad_url.is_not(None),
                        ),
                        DailyFeaturedPublication.deactivated_at.is_(None),
                    )
                    .order_by(DailyFeaturedPublication.created_at.desc())
                )
            ).all()
            apartment_ids = {
                apartment_id
                for apartment_id, _, _, _ in references
                if apartment_id is not None
            }
            lalafo_ids = {lalafo_id for _, lalafo_id, _, _ in references}
            if not apartment_ids and not lalafo_ids:
                return []
            rows = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            or_(
                                Apartment.id.in_(apartment_ids),
                                Apartment.lalafo_id.in_(lalafo_ids),
                            ),
                            Apartment.rooms.in_(("1", "2", "studio")),
                            Apartment.active.is_(True),
                            Apartment.phone != "",
                        )
                    )
                ).all()
            )
        by_id = {item.id: item for item in rows}
        by_lalafo_id = {item.lalafo_id: item for item in rows}
        selected: list[ManagedLalafoSource] = []
        used_source_ids: set[int] = set()
        for source_id, source_lalafo_id, managed_id, managed_url in references:
            apartment = by_id.get(source_id) or by_lalafo_id.get(source_lalafo_id)
            if apartment is None or apartment.id in used_source_ids:
                continue
            selected.append(
                ManagedLalafoSource(
                    apartment=apartment,
                    managed_lalafo_ad_id=managed_id,
                    managed_lalafo_ad_url=managed_url,
                )
            )
            used_source_ids.add(apartment.id)
        return selected

    async def verified_source_by_photos(
        self,
        photo_urls: list[str],
        *,
        excluded_lalafo_ids: set[int] | None = None,
    ) -> Apartment | None:
        """Find one verified source whose photos are identical to a managed ad.

        Lalafo keeps the same content-addressed image filename when a photo is
        reused.  Requiring at least two identical files and a unique best match
        prevents a visually similar apartment from donating the wrong phone.
        """

        def photo_key(url: str) -> str:
            return PurePosixPath(urlsplit(url).path).name.casefold()

        keys = {photo_key(url) for url in photo_urls if photo_key(url)}
        if len(keys) < 2:
            return None
        excluded = excluded_lalafo_ids or set()
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Apartment).where(
                            Apartment.phone != "",
                            Apartment.phone_source_version == PHONE_SOURCE_VERSION,
                        )
                    )
                ).all()
            )
        scored: list[tuple[int, Apartment]] = []
        for apartment in rows:
            if apartment.lalafo_id in excluded:
                continue
            candidate_keys = {
                photo_key(url) for url in apartment.photo_urls if photo_key(url)
            }
            overlap = len(keys & candidate_keys)
            if overlap >= 2:
                scored.append((overlap, apartment))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        best_overlap = scored[0][0]
        best = [apartment for overlap, apartment in scored if overlap == best_overlap]
        return best[0] if len(best) == 1 else None

    async def save_managed_lalafo_source(
        self,
        *,
        managed_lalafo_ad_id: int,
        managed_lalafo_ad_url: str,
        apartment: Apartment,
    ) -> None:
        """Persist a verified managed-to-original link for future rotations."""
        business_date = datetime.now(timezone.utc).date()
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(DailyFeaturedPublication).where(
                    DailyFeaturedPublication.managed_lalafo_ad_id
                    == managed_lalafo_ad_id
                )
            )
            if row is None:
                row = DailyFeaturedPublication(
                    business_date=business_date,
                    slot=-managed_lalafo_ad_id,
                    source_apartment_id=apartment.id,
                    source_lalafo_id=apartment.lalafo_id,
                    managed_lalafo_ad_id=managed_lalafo_ad_id,
                    managed_lalafo_ad_url=managed_lalafo_ad_url,
                    campaign_status="active",
                    lalafo_publication_status="published",
                )
                session.add(row)
            else:
                row.source_apartment_id = apartment.id
                row.source_lalafo_id = apartment.lalafo_id
                row.managed_lalafo_ad_url = managed_lalafo_ad_url
                row.deactivated_at = None

    async def published_lalafo_ids(self, lalafo_ids: list[int]) -> set[int]:
        if not lalafo_ids:
            return set()
        async with self.sessions() as session:
            result = await session.scalars(
                select(Apartment.lalafo_id).where(
                    Apartment.lalafo_id.in_(lalafo_ids),
                    Apartment.publication_status == "published",
                )
            )
            return set(result.all())

    async def repostable_lalafo_ids(
        self, lalafo_ids: list[int], *, after_hours: float
    ) -> set[int]:
        """Return published ads whose latest group card is old enough to rotate in again."""
        return set(
            await self.repostable_lalafo_publications(
                lalafo_ids, after_hours=after_hours
            )
        )

    async def repostable_lalafo_publications(
        self, lalafo_ids: list[int], *, after_hours: float
    ) -> dict[int, datetime]:
        """Return eligible repost IDs with their last publication time.

        The timestamps let the publisher choose the least recently shown cards
        instead of repeating the same highest-scoring apartments every cycle.
        """
        if not lalafo_ids:
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, after_hours))
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(Apartment.lalafo_id, Apartment.published_at).where(
                        Apartment.lalafo_id.in_(lalafo_ids),
                        Apartment.publication_status == "published",
                        or_(Apartment.active.is_(True), Apartment.availability_reason == "listing_age_limit"),
                        Apartment.published_at.is_not(None),
                        Apartment.published_at <= cutoff,
                    )
                )
            ).all()
            return {
                int(lalafo_id): published_at
                for lalafo_id, published_at in rows
                if published_at is not None
            }

    async def is_duplicate(self, ad: LalafoAd, *, repost_before: datetime | None = None) -> bool:
        return ad.lalafo_id in await self.duplicate_candidate_ids([ad], repost_before=repost_before)


    async def duplicate_candidate_ids(self, ads: list[LalafoAd], *, repost_before: datetime | None = None) -> set[int]:
        """Resolve all ID/fingerprint/contact duplicates in one database trip."""
        if not ads:
            return set()
        rooms = {ad.rooms for ad in ads}
        lalafo_ids = {ad.lalafo_id for ad in ads}
        async with self.sessions() as session:
            published = (
                await session.execute(
                    select(Apartment.lalafo_id, Apartment.rooms, Apartment.district,
                           Apartment.price, Apartment.phone, Apartment.photo_urls).where(
                        Apartment.publication_status == "published",
                        or_(Apartment.rooms.in_(rooms), Apartment.lalafo_id.in_(lalafo_ids)),
                        or_(Apartment.published_at.is_(None), Apartment.published_at > repost_before)
                        if repost_before is not None else True,
                    )
                )
            ).all()
        index = ListingIndex(published)
        return {ad.lalafo_id for ad in ads if index.contains(ad)}

    async def upsert_discovered(
        self, ad: LalafoAd, *, discovery_priority: bool = False
    ) -> Apartment:
        try:
            return await self._upsert_discovered_once(ad, discovery_priority=discovery_priority)
        except IntegrityError:
            # Independent collectors can discover the same source simultaneously.
            # The losing insert reads and updates the row created by the winner.
            return await self._upsert_discovered_once(ad, discovery_priority=discovery_priority)

    async def _upsert_discovered_once(
        self, ad: LalafoAd, *, discovery_priority: bool
    ) -> Apartment:
        if not ad.district and ad.price >= 25_000:
            ad = ad.model_copy(update={"district": "Центр"})
        fingerprint = ad_fingerprint(ad)
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            result = await session.execute(
                select(Apartment).where(Apartment.lalafo_id == ad.lalafo_id).with_for_update()
            )
            apartment = result.scalar_one_or_none()
            if apartment is None:
                apartment = Apartment(
                    lalafo_id=ad.lalafo_id,
                    source_url=ad.source_url,
                    source_description=ad.source_description,
                    phone=ad.phone,
                    phone_source_version=PHONE_SOURCE_VERSION,
                    fingerprint=fingerprint,
                    price=ad.price,
                    rooms=ad.rooms,
                    district=ad.district,
                    city=ad.city,
                    deposit=ad.deposit,
                    no_subletting=ad.no_subletting,
                    owner_listing=ad.owner_listing,
                    seller_type=(
                        ad.seller_type
                        if ad.seller_type != "unknown"
                        else ("owner" if ad.owner_listing else "unknown")
                    ),
                    discovery_priority=discovery_priority,
                    photo_urls=ad.photo_urls,
                    source_updated_at=ad.source_updated_at,
                    last_seen_at=now,
                    active=True,
                    publication_status="discovered",
                )
                session.add(apartment)
            else:
                apartment.source_url = ad.source_url
                apartment.source_description = ad.source_description
                apartment.phone = ad.phone
                apartment.phone_source_version = PHONE_SOURCE_VERSION
                apartment.fingerprint = fingerprint
                apartment.price = ad.price
                apartment.rooms = ad.rooms
                apartment.district = ad.district
                apartment.city = ad.city
                apartment.deposit = ad.deposit
                apartment.no_subletting = ad.no_subletting
                apartment.owner_listing = ad.owner_listing
                apartment.seller_type = (
                    ad.seller_type
                    if ad.seller_type != "unknown"
                    else ("owner" if ad.owner_listing else "unknown")
                )
                apartment.discovery_priority = (
                    apartment.discovery_priority or discovery_priority
                )
                apartment.photo_urls = ad.photo_urls
                apartment.source_updated_at = ad.source_updated_at
                apartment.last_seen_at = now
                apartment.active = True
                if apartment.availability_reason == "listing_age_limit":
                    apartment.availability_status = "active"
                    apartment.availability_checked_at = now
                    apartment.availability_reason = "source_revalidated"
            await session.flush()
            await session.refresh(apartment)
            return apartment

    async def mark_published(
        self, apartment_id: int, *, chat_id: int, message_id: int,
        promote_requested: bool = False,
        acknowledged_at: datetime | None = None,
    ) -> Apartment:
        async with self.sessions.begin() as session:
            apartment = await session.get(Apartment, apartment_id)
            if apartment is None:
                raise LookupError("Apartment not found")
            from app.models import RequestedCardPromotion, RequestedCardPublication
            from app.promotions import record_delivery, register_requested
            now = acknowledged_at or datetime.now(timezone.utc)
            existing_receipt = await session.scalar(select(RequestedCardPublication).where(
                RequestedCardPublication.telegram_chat_id == chat_id,
                RequestedCardPublication.telegram_message_id == message_id,
            ))
            if existing_receipt is not None:
                if existing_receipt.apartment_id != apartment_id:
                    raise RuntimeError("Publication receipt belongs to another apartment")
                return apartment
            if promote_requested:
                await register_requested(session, apartment, now=now)
            if promote_requested or await session.get(RequestedCardPromotion, apartment_id) is not None:
                added = await record_delivery(session, apartment, chat_id=chat_id,
                                              message_id=message_id, published_at=now)
                if not added:
                    return apartment
            apartment.telegram_chat_id = chat_id
            apartment.telegram_message_id = message_id
            apartment.published_at = now
            apartment.publication_status = "published"
            apartment.active = True
            apartment.availability_status = "active"
            apartment.availability_checked_at = apartment.published_at
            apartment.availability_reason = "publication_age_window"
            apartment.keyboard_version = APARTMENT_KEYBOARD_VERSION
            await session.flush()
            await session.refresh(apartment)
            return apartment

    async def mark_inactive(self, apartment_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Apartment).where(Apartment.id == apartment_id).values(active=False)
            )

    async def set_availability(
        self, apartment_id: int, *, status: str, checked_at: datetime, reason: str
    ) -> Apartment | None:
        async with self.sessions.begin() as session:
            apartment = await session.get(Apartment, apartment_id)
            if apartment is None:
                return None
            apartment.availability_status = status
            apartment.availability_checked_at = checked_at
            apartment.availability_reason = reason
            if status == "active":
                apartment.active = True
            elif status == "unavailable":
                apartment.active = False
                await session.execute(
                    update(ApartmentInventoryQueue)
                    .where(
                        ApartmentInventoryQueue.apartment_id == apartment_id,
                        ApartmentInventoryQueue.status.in_(("queued", "publishing")),
                    )
                    .values(status="skipped", last_error="source_unavailable")
                )
            await session.flush()
            await session.refresh(apartment)
            return apartment

    async def published_count(self) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(Apartment).where(
                        Apartment.publication_status == "published"
                    )
                )
                or 0
            )


class PaymentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get_access(self, user_id: int, apartment_id: int) -> PaymentRequest | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest)
                .options(selectinload(PaymentRequest.apartment))
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.apartment_id == apartment_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_request(self, request_id: int) -> PaymentRequest | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest)
                .options(selectinload(PaymentRequest.apartment))
                .where(PaymentRequest.id == request_id)
            )
            return result.scalar_one_or_none()

    async def active_weekly_access(self, user_id: int) -> PaymentRequest | PaymentHistory | None:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.status == "approved",
                    or_(
                        and_(PaymentRequest.plan == LIFETIME_PLAN, PaymentRequest.access_expires_at.is_(None)),
                        and_(PaymentRequest.plan.in_((WEEK_PLAN, MONTH_PLAN)), PaymentRequest.access_expires_at > now),
                    ),
                )
                .order_by((PaymentRequest.plan == LIFETIME_PLAN).desc(), PaymentRequest.access_expires_at.desc())
                .limit(1)
            )
            active = result.scalar_one_or_none()
            if active is not None:
                return active
            # Apartment cleanup deletes checkouts, and older checkout retries
            # could reset an approved row. Preserve the paid grant in both cases,
            # while respecting explicit rejection and expiry on approved rows.
            return await session.scalar(
                select(PaymentHistory)
                .where(
                    PaymentHistory.telegram_user_id == user_id,
                    or_(
                        and_(
                            PaymentHistory.plan == LIFETIME_PLAN,
                            PaymentHistory.access_expires_at.is_(None),
                        ),
                        and_(
                            PaymentHistory.plan.in_((WEEK_PLAN, MONTH_PLAN)),
                            PaymentHistory.access_expires_at > now,
                        ),
                    ),
                    ~select(PaymentRequest.id).where(
                        PaymentRequest.id == PaymentHistory.payment_request_id,
                        PaymentRequest.status.in_(("approved", "rejected")),
                    ).exists(),
                )
                .order_by(PaymentHistory.paid_at.desc())
                .limit(1)
            )

    async def submit(
        self,
        *,
        user_id: int,
        apartment_id: int,
        username: str | None,
        first_name: str | None,
        plan: str = LIFETIME_PLAN,
        prepare_only: bool = False,
    ) -> PaymentSubmission:
        if plan not in {WEEK_PLAN, MONTH_PLAN, LIFETIME_PLAN}:
            raise ValueError("Unsupported access plan")
        try:
            return await self._submit_once(
                user_id=user_id,
                apartment_id=apartment_id,
                username=username,
                first_name=first_name,
                plan=plan,
                prepare_only=prepare_only,
            )
        except IntegrityError:
            # A concurrent click may win the unique (user, apartment) insert.
            # Retrying reads that row and returns "pending" instead of failing.
            return await self._submit_once(
                user_id=user_id,
                apartment_id=apartment_id,
                username=username,
                first_name=first_name,
                plan=plan,
                prepare_only=prepare_only,
            )

    async def _submit_once(
        self,
        *,
        user_id: int,
        apartment_id: int,
        username: str | None,
        first_name: str | None,
        plan: str,
        prepare_only: bool,
    ) -> PaymentSubmission:
        async with self.sessions.begin() as session:
            apartment = await session.get(Apartment, apartment_id)
            if (
                apartment is None
                or not apartment.phone
                or apartment.availability_status == "unavailable"
                or listing_expired(apartment)
            ):
                raise LookupError("Apartment is unavailable")
            result = await session.execute(
                select(PaymentRequest).where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.apartment_id == apartment_id,
                ).with_for_update()
            )
            request = result.scalar_one_or_none()
            expires_at = request.access_expires_at if request is not None else None
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if request is None:
                request = PaymentRequest(
                    telegram_user_id=user_id,
                    apartment_id=apartment_id,
                    username=username,
                    first_name=first_name,
                    plan=plan,
                    status="prepared" if prepare_only else "awaiting_receipt",
                )
                session.add(request)
                outcome = "created"
            elif request.status == "approved" and (
                (request.plan == LIFETIME_PLAN and expires_at is None)
                or (
                    request.plan in (WEEK_PLAN, MONTH_PLAN)
                    and expires_at is not None
                    and expires_at > datetime.now(timezone.utc)
                )
            ):
                outcome = "approved"
            elif request.status == "prepared" and request.plan == plan:
                if not prepare_only:
                    request.status = "awaiting_receipt"
                outcome = request.status
            elif request.status == "pending" and request.plan == plan:
                outcome = "pending"
            elif request.status == "awaiting_receipt" and request.plan == plan:
                outcome = "awaiting_receipt"
            else:
                request.status = "prepared" if prepare_only else "awaiting_receipt"
                request.plan = plan
                request.username = username
                request.first_name = first_name
                request.created_at = datetime.now(timezone.utc)
                request.receipt_file_id = None
                request.receipt_file_type = None
                request.approved_at = None
                request.approved_by = None
                request.access_expires_at = None
                request.rejected_at = None
                request.rejected_by = None
                request.admin_message_id = None
                request.admin_notification_token = None
                request.admin_notification_claimed_at = None
                request.payment_reminder_sent_at = None
                request.payment_reminder_claimed_at = None
                request.provider_payment_id = None
                request.provider_payment_url = None
                request.provider_status = None
                request.payment_claimed_at = None
                outcome = "created"
            await session.flush()
            await session.refresh(request)
            return PaymentSubmission(request=request, outcome=outcome)


    async def mark_payment_claimed(
        self, *, user_id: int, apartment_id: int
    ) -> PaymentRequest | None:
        """Queue a customer payment claim for manual administrator review."""
        async with self.sessions.begin() as session:
            request = await session.scalar(
                select(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.apartment_id == apartment_id,
                )
                .order_by(PaymentRequest.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if request is None:
                return None
            if request.status == "approved":
                return request
            if request.status not in {"awaiting_receipt", "pending"}:
                return request
            request.status = "pending"
            request.payment_claimed_at = None
            request_id = request.id
        return await self.get_request(request_id)

    async def set_admin_message(self, request_id: int, message_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(PaymentRequest)
                .where(PaymentRequest.id == request_id)
                .values(admin_message_id=message_id)
            )

    @staticmethod
    def _notification_due(now: datetime):
        return or_(
            PaymentRequest.admin_message_id.is_(None),
            and_(PaymentRequest.admin_message_id == -1,
                or_(PaymentRequest.admin_notification_claimed_at.is_(None),
                    PaymentRequest.admin_notification_claimed_at <= now - timedelta(minutes=5))),
        )

    async def claim_admin_notification(self, request_id: int, *, token: str | None = None) -> bool:
        """Atomically reserve one admin notification send for a pending request."""
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.status == "pending",
                    self._notification_due(now),
                )
                .values(admin_message_id=-1, admin_notification_token=token or uuid4().hex,
                        admin_notification_claimed_at=now)
            )
            return result.rowcount == 1

    async def finish_admin_notification(self, request_id: int, message_id: int, *, token: str | None = None) -> bool:
        async with self.sessions.begin() as session:
            conditions = [PaymentRequest.id == request_id, PaymentRequest.admin_message_id == -1]
            if token is not None:
                conditions.append(PaymentRequest.admin_notification_token == token)
            result = await session.execute(
                update(PaymentRequest)
                .where(*conditions)
                .values(admin_message_id=message_id, admin_notification_token=None,
                        admin_notification_claimed_at=None)
            )
            return result.rowcount == 1

    async def release_admin_notification(self, request_id: int, *, token: str | None = None) -> None:
        """Allow a later click to retry after Telegram notification failure."""
        async with self.sessions.begin() as session:
            conditions = [PaymentRequest.id == request_id, PaymentRequest.admin_message_id == -1]
            if token is not None:
                conditions.append(PaymentRequest.admin_notification_token == token)
            await session.execute(
                update(PaymentRequest)
                .where(*conditions)
                .values(admin_message_id=None, admin_notification_token=None,
                        admin_notification_claimed_at=None)
            )


    async def decide(self, request_id: int, *, approve: bool, admin_id: int, expected_admin_message_id: int | None = None) -> str:
        if admin_id <= 0:
            return "forbidden"
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            current = await session.scalar(select(PaymentRequest).where(PaymentRequest.id == request_id).with_for_update())
            if current is None:
                return "missing"
            if expected_admin_message_id is not None and current.admin_message_id != expected_admin_message_id:
                return "stale"
            if current.status != "pending":
                return f"already_{current.status}"
            if approve:
                self._approve_request(session, current, now=now, admin_id=admin_id)
                # One weekly purchase unlocks every apartment. A customer may
                # open several cards and submit several claims before the
                # administrator reviews the first one. Close those sibling
                # claims under the same grant so later button clicks cannot be
                # counted as additional payments or extend access repeatedly.
                await session.execute(
                    update(PaymentRequest)
                    .where(
                        PaymentRequest.telegram_user_id == current.telegram_user_id,
                        PaymentRequest.id != current.id,
                        PaymentRequest.plan == current.plan,
                        PaymentRequest.status.in_(("prepared", "awaiting_receipt", "pending")),
                    )
                    .values(
                        status="approved",
                        approved_at=now,
                        approved_by=admin_id,
                        access_expires_at=current.access_expires_at,
                        rejected_at=None,
                        rejected_by=None,
                        admin_notification_token=None,
                        admin_notification_claimed_at=None,
                    )
                )
                return "approved"
            current.status = "rejected"
            current.rejected_at = now
            current.rejected_by = admin_id
            current.access_expires_at = None
            return "rejected"

    @staticmethod
    def _approve_request(session, current: PaymentRequest, *, now: datetime, admin_id: int) -> None:
        if admin_id <= 0:
            raise ValueError("Administrator approval is required")
        current.status = "approved"
        current.approved_at = now
        current.approved_by = admin_id
        current.access_expires_at = expires_at_for(current.plan, now)
        if current.access_expires_at is not None or current.plan == LIFETIME_PLAN:
            session.add(
                PaymentHistory(
                    payment_request_id=current.id,
                    telegram_user_id=current.telegram_user_id,
                    username=current.username,
                    first_name=current.first_name,
                    apartment_id=current.apartment_id,
                    plan=current.plan,
                    amount=plan_price(current.plan),
                    provider_payment_id=(
                        current.provider_payment_id
                        or f"manual-{current.id}-{int(now.timestamp())}"
                    ),
                    paid_at=now,
                    access_expires_at=current.access_expires_at,
                )
            )

    async def pending_notifications(self, limit: int = 50) -> list[PaymentRequest]:
        async with self.sessions() as session:
            return list((await session.scalars(
                select(PaymentRequest)
                .where(PaymentRequest.status == "pending", self._notification_due(datetime.now(timezone.utc)))
                .order_by(PaymentRequest.created_at.asc())
                .limit(limit)
            )).all())

    async def claim_payment_reminders(
        self,
        *,
        now: datetime | None = None,
        delay: timedelta = timedelta(minutes=6),
        limit: int = 25,
    ) -> list[PaymentRequest]:
        """Reserve one recovery message per customer with an abandoned checkout."""
        current = now or datetime.now(timezone.utc)
        stale_claim = current - timedelta(minutes=5)
        recent_cutoff = current - timedelta(days=2)
        due = (
            select(func.min(PaymentRequest.id).label("id"))
            .where(
                PaymentRequest.status == "awaiting_receipt",
                PaymentRequest.created_at <= current - delay,
                PaymentRequest.created_at >= recent_cutoff,
                PaymentRequest.payment_reminder_sent_at.is_(None),
                or_(
                    PaymentRequest.payment_reminder_claimed_at.is_(None),
                    PaymentRequest.payment_reminder_claimed_at <= stale_claim,
                ),
            )
            .group_by(PaymentRequest.telegram_user_id)
        )
        async with self.sessions.begin() as session:
            requests = list(
                (
                    await session.scalars(
                        select(PaymentRequest)
                        .options(selectinload(PaymentRequest.apartment))
                        .where(PaymentRequest.id.in_(due))
                        .order_by(PaymentRequest.created_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for request in requests:
                request.payment_reminder_claimed_at = current
            return requests

    async def finish_payment_reminder(
        self, request_id: int, *, sent_at: datetime | None = None
    ) -> None:
        """Mark all current checkouts for this customer as already recovered."""
        current = sent_at or datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            user_id = await session.scalar(
                select(PaymentRequest.telegram_user_id).where(
                    PaymentRequest.id == request_id
                )
            )
            if user_id is None:
                return
            await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.status == "awaiting_receipt",
                    PaymentRequest.payment_reminder_sent_at.is_(None),
                )
                .values(
                    payment_reminder_sent_at=current,
                    payment_reminder_claimed_at=None,
                )
            )

    async def release_payment_reminder(self, request_id: int) -> None:
        """Make a transiently failed recovery message eligible for another try."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.payment_reminder_sent_at.is_(None),
                )
                .values(payment_reminder_claimed_at=None)
            )

    async def prepare_provider_payment(
        self,
        request_id: int,
        payment_id: str,
        *,
        configuration_id: str | None = None,
    ) -> PaymentRequest:
        """Reserve a stable Finik id and replace links from an old merchant."""
        async with self.sessions.begin() as session:
            current = await session.get(PaymentRequest, request_id, with_for_update=True)
            if current is None:
                raise LookupError("Payment request is unavailable")
            expected_statuses = (
                {
                    f"created:{configuration_id}",
                    f"waiting:{configuration_id}",
                }
                if configuration_id
                else set()
            )
            configuration_changed = bool(
                configuration_id and current.provider_status not in expected_statuses
            )
            now = datetime.now(timezone.utc)
            created_at = current.created_at.replace(tzinfo=timezone.utc) if current.created_at.tzinfo is None else current.created_at
            checkout_expired = (
                current.status in {"prepared", "awaiting_receipt"}
                and current.provider_payment_id is not None
                and now - created_at >= timedelta(minutes=5)
                and current.provider_status != "succeeded"
            )
            if current.status != "approved" and current.provider_status != "succeeded" and (
                not current.provider_payment_id or configuration_changed or checkout_expired
            ):
                current.provider_payment_id = payment_id
                current.provider_payment_url = None
                current.created_at = now
                current.provider_status = (
                    f"created:{configuration_id}" if configuration_id else "created"
                )
                await session.flush()
            reserved_id = current.id
        request = await self.get_request(reserved_id)
        if request is None:
            raise LookupError("Payment request is unavailable")
        return request

    async def set_provider_payment_url(
        self,
        request_id: int,
        payment_url: str,
        *,
        configuration_id: str | None = None,
        payment_id: str | None = None,
    ) -> bool:
        async with self.sessions.begin() as session:
            statement = update(PaymentRequest).where(PaymentRequest.id == request_id)
            if payment_id is not None:
                statement = statement.where(PaymentRequest.provider_payment_id == payment_id)
            # A slow checkout response must not erase an acknowledged payment.
            result = await session.execute(
                statement.where(
                    PaymentRequest.status != "approved",
                    or_(PaymentRequest.provider_status.is_(None),
                        PaymentRequest.provider_status.not_in(("succeeded", "amount_mismatch", "failed"))),
                ).values(provider_payment_url=payment_url,
                    provider_status=f"waiting:{configuration_id}" if configuration_id else "waiting")
            )
            return result.rowcount == 1

    async def get_by_provider_payment_id(self, payment_id: str) -> PaymentRequest | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest).where(PaymentRequest.provider_payment_id == payment_id)
            )
            return result.scalar_one_or_none()

    async def apply_provider_result(
        self, payment_id: str, *, succeeded: bool, amount: int | float | None
    ) -> tuple[str, PaymentRequest | None]:
        """Record the provider result without bypassing administrator approval."""
        async with self.sessions.begin() as session:
            result = await session.execute(
                select(PaymentRequest)
                .where(PaymentRequest.provider_payment_id == payment_id)
                .with_for_update()
            )
            current = result.scalar_one_or_none()
            if current is None:
                return "missing", None
            expected = plan_price(current.plan)
            try:
                amount_matches = amount is not None and Decimal(str(amount)) == Decimal(expected)
            except (InvalidOperation, ValueError, TypeError):
                amount_matches = False
            if succeeded and not amount_matches:
                if current.status != "approved" and current.provider_status != "succeeded":
                    current.provider_status = "amount_mismatch"
                return "amount_mismatch", current
            if current.status == "approved":
                return "already_approved", current
            if not succeeded:
                if current.provider_status == "succeeded":
                    return "already_succeeded", current
                current.provider_status = "failed"
                return "failed", current
            current.provider_status = "succeeded"
            # A signed Finik callback with the exact amount is stronger than
            # the customer's manual claim. Queue it for the same administrator
            # review without requiring the customer to return and click again.
            if current.status in {"prepared", "awaiting_receipt"}:
                current.status = "pending"
                current.payment_claimed_at = datetime.now(timezone.utc)
            return ("pending" if current.status == "pending" else "awaiting_confirmation"), current

    async def pending(self, limit: int = 20) -> list[PaymentRequest]:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest)
                .options(selectinload(PaymentRequest.apartment))
                .where(PaymentRequest.status == "pending")
                .order_by(PaymentRequest.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars())

    async def recent_paid(self, limit: int = 30) -> list[PaymentHistory]:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentHistory)
                .order_by(PaymentHistory.paid_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def counts(self) -> dict[str, int]:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest.status, func.count()).group_by(PaymentRequest.status)
            )
            counts = {"pending": 0, "approved": 0, "rejected": 0}
            counts.update({str(status): int(count) for status, count in result})
            return counts

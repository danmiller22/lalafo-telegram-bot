from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.models import Apartment, PaymentRequest
from app.payment_plans import WEEK_PLAN, expires_at_for
from app.state import ad_fingerprint
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION


@dataclass(frozen=True)
class PaymentSubmission:
    request: PaymentRequest
    outcome: str


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
        if not lalafo_ids:
            return set()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, after_hours))
        async with self.sessions() as session:
            result = await session.scalars(
                select(Apartment.lalafo_id).where(
                    Apartment.lalafo_id.in_(lalafo_ids),
                    Apartment.publication_status == "published",
                    Apartment.active.is_(True),
                    Apartment.published_at.is_not(None),
                    Apartment.published_at <= cutoff,
                )
            )
            return set(result.all())

    async def is_duplicate(self, ad: LalafoAd) -> bool:
        fingerprint = ad_fingerprint(ad)
        async with self.sessions() as session:
            result = await session.execute(
                select(Apartment.id).where(
                    (
                        (Apartment.lalafo_id == ad.lalafo_id)
                        & (Apartment.publication_status == "published")
                    )
                    | (
                        (Apartment.fingerprint == fingerprint)
                        & (Apartment.publication_status == "published")
                    )
                    | (
                        (Apartment.phone == ad.phone)
                        & (Apartment.publication_status == "published")
                        & (Apartment.active.is_(True))
                    )
                )
            )
            return result.first() is not None

    async def upsert_discovered(self, ad: LalafoAd) -> Apartment:
        fingerprint = ad_fingerprint(ad)
        async with self.sessions.begin() as session:
            result = await session.execute(
                select(Apartment).where(Apartment.lalafo_id == ad.lalafo_id)
            )
            apartment = result.scalar_one_or_none()
            if apartment is None:
                apartment = Apartment(
                    lalafo_id=ad.lalafo_id,
                    source_url=ad.source_url,
                    phone=ad.phone,
                    phone_source_version=PHONE_SOURCE_VERSION,
                    fingerprint=fingerprint,
                    price=ad.price,
                    rooms=ad.rooms,
                    district=ad.district,
                    city=ad.city,
                    deposit=ad.deposit,
                    no_subletting=ad.no_subletting,
                    photo_urls=ad.photo_urls,
                    source_updated_at=ad.source_updated_at,
                    active=True,
                    publication_status="discovered",
                )
                session.add(apartment)
            else:
                apartment.source_url = ad.source_url
                apartment.phone = ad.phone
                apartment.phone_source_version = PHONE_SOURCE_VERSION
                apartment.fingerprint = fingerprint
                apartment.price = ad.price
                apartment.rooms = ad.rooms
                apartment.district = ad.district
                apartment.city = ad.city
                apartment.deposit = ad.deposit
                apartment.no_subletting = ad.no_subletting
                apartment.photo_urls = ad.photo_urls
                apartment.source_updated_at = ad.source_updated_at
                apartment.active = True
            await session.flush()
            await session.refresh(apartment)
            return apartment

    async def mark_published(
        self, apartment_id: int, *, chat_id: int, message_id: int
    ) -> Apartment:
        async with self.sessions.begin() as session:
            apartment = await session.get(Apartment, apartment_id)
            if apartment is None:
                raise LookupError("Apartment not found")
            apartment.telegram_chat_id = chat_id
            apartment.telegram_message_id = message_id
            apartment.published_at = datetime.now(timezone.utc)
            apartment.publication_status = "published"
            apartment.keyboard_version = APARTMENT_KEYBOARD_VERSION
            await session.flush()
            await session.refresh(apartment)
            return apartment

    async def mark_inactive(self, apartment_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Apartment).where(Apartment.id == apartment_id).values(active=False)
            )

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

    async def active_weekly_access(self, user_id: int) -> PaymentRequest | None:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.plan == WEEK_PLAN,
                    PaymentRequest.status == "approved",
                    PaymentRequest.access_expires_at.is_not(None),
                    PaymentRequest.access_expires_at > now,
                )
                .order_by(PaymentRequest.access_expires_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def submit(
        self,
        *,
        user_id: int,
        apartment_id: int,
        username: str | None,
        first_name: str | None,
        plan: str = WEEK_PLAN,
    ) -> PaymentSubmission:
        if plan != WEEK_PLAN:
            raise ValueError("Only weekly access is available")
        try:
            return await self._submit_once(
                user_id=user_id,
                apartment_id=apartment_id,
                username=username,
                first_name=first_name,
                plan=plan,
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
            )

    async def _submit_once(
        self,
        *,
        user_id: int,
        apartment_id: int,
        username: str | None,
        first_name: str | None,
        plan: str,
    ) -> PaymentSubmission:
        async with self.sessions.begin() as session:
            apartment = await session.get(Apartment, apartment_id)
            if apartment is None or not apartment.active or not apartment.phone:
                raise LookupError("Apartment is unavailable")
            result = await session.execute(
                select(PaymentRequest).where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.apartment_id == apartment_id,
                )
            )
            request = result.scalar_one_or_none()
            if request is None:
                request = PaymentRequest(
                    telegram_user_id=user_id,
                    apartment_id=apartment_id,
                    username=username,
                    first_name=first_name,
                    plan=plan,
                    status="awaiting_receipt",
                )
                session.add(request)
                outcome = "created"
            elif request.status == "pending":
                outcome = "pending"
            elif request.status == "awaiting_receipt" and request.plan == plan:
                outcome = "awaiting_receipt"
            else:
                request.status = "awaiting_receipt"
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
                outcome = "created"
            await session.flush()
            await session.refresh(request)
            return PaymentSubmission(request=request, outcome=outcome)

    async def submit_receipt(
        self, *, user_id: int, file_id: str, file_type: str
    ) -> PaymentRequest | None:
        async with self.sessions.begin() as session:
            result = await session.execute(
                select(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.status == "awaiting_receipt",
                )
                .order_by(PaymentRequest.created_at.desc())
                .limit(1)
            )
            request = result.scalar_one_or_none()
            if request is None:
                return None
            request.receipt_file_id = file_id
            request.receipt_file_type = file_type
            request.status = "pending"
            request.admin_message_id = None
            await session.flush()
            request_id = request.id
        return await self.get_request(request_id)

    async def set_admin_message(self, request_id: int, message_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(PaymentRequest)
                .where(PaymentRequest.id == request_id)
                .values(admin_message_id=message_id)
            )

    async def claim_admin_notification(self, request_id: int) -> bool:
        """Atomically reserve one admin notification send for a pending request."""
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.status == "pending",
                    PaymentRequest.admin_message_id.is_(None),
                )
                .values(admin_message_id=-1)
            )
            return result.rowcount == 1

    async def finish_admin_notification(self, request_id: int, message_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.admin_message_id == -1,
                )
                .values(admin_message_id=message_id)
            )
            return result.rowcount == 1

    async def release_admin_notification(self, request_id: int) -> None:
        """Allow a later click to retry after Telegram notification failure."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(PaymentRequest)
                .where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.admin_message_id == -1,
                )
                .values(admin_message_id=None)
            )

    async def decide(self, request_id: int, *, approve: bool, admin_id: int) -> str:
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            current = await session.get(PaymentRequest, request_id)
            if current is None:
                return "missing"
            if current.status != "pending":
                return f"already_{current.status}"
            if approve:
                current.status = "approved"
                current.approved_at = now
                current.approved_by = admin_id
                current.access_expires_at = expires_at_for(current.plan, now)
                return "approved"
            current.status = "rejected"
            current.rejected_at = now
            current.rejected_by = admin_id
            current.access_expires_at = None
            return "rejected"

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

    async def counts(self) -> dict[str, int]:
        async with self.sessions() as session:
            result = await session.execute(
                select(PaymentRequest.status, func.count()).group_by(PaymentRequest.status)
            )
            counts = {"pending": 0, "approved": 0, "rejected": 0}
            counts.update({str(status): int(count) for status, count in result})
            return counts

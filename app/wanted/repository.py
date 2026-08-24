from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import WantedAd


class WantedAdRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        rooms: str,
        district: str,
        budget: int,
        move_in: str,
        tenants: str,
        notes: str,
        contact: str,
    ) -> WantedAd:
        async with self.sessions.begin() as session:
            ad = WantedAd(
                telegram_user_id=user_id,
                username=username,
                first_name=first_name,
                rooms=rooms,
                district=district,
                budget=budget,
                move_in=move_in,
                tenants=tenants,
                notes=notes,
                contact=contact,
                status="awaiting_payment",
            )
            session.add(ad)
            await session.flush()
            await session.refresh(ad)
            return ad

    async def get(self, ad_id: int) -> WantedAd | None:
        async with self.sessions() as session:
            return await session.get(WantedAd, ad_id)

    async def get_owned(self, ad_id: int, user_id: int) -> WantedAd | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(WantedAd).where(
                    WantedAd.id == ad_id,
                    WantedAd.telegram_user_id == user_id,
                )
            )
            return result.scalar_one_or_none()

    async def submit_payment(self, ad_id: int, user_id: int) -> tuple[str, WantedAd | None]:
        async with self.sessions.begin() as session:
            result = await session.execute(
                select(WantedAd).where(
                    WantedAd.id == ad_id,
                    WantedAd.telegram_user_id == user_id,
                )
            )
            ad = result.scalar_one_or_none()
            if ad is None:
                return "missing", None
            if ad.status in {"published", "publishing"}:
                return ad.status, ad
            if ad.status == "pending":
                return "pending", ad
            ad.status = "pending"
            ad.admin_message_id = None
            ad.rejected_at = None
            ad.rejected_by = None
            ad.updated_at = datetime.now(timezone.utc)
            await session.flush()
            await session.refresh(ad)
            return "created", ad

    async def claim_admin_notification(self, ad_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(WantedAd)
                .where(
                    WantedAd.id == ad_id,
                    WantedAd.status == "pending",
                    WantedAd.admin_message_id.is_(None),
                )
                .values(admin_message_id=-1)
            )
            return result.rowcount == 1

    async def finish_admin_notification(self, ad_id: int, message_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(WantedAd)
                .where(WantedAd.id == ad_id, WantedAd.admin_message_id == -1)
                .values(admin_message_id=message_id)
            )
            return result.rowcount == 1

    async def release_admin_notification(self, ad_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(WantedAd)
                .where(WantedAd.id == ad_id, WantedAd.admin_message_id == -1)
                .values(admin_message_id=None)
            )

    async def begin_decision(self, ad_id: int, *, approve: bool, admin_id: int) -> str:
        now = datetime.now(timezone.utc)
        values = (
            {"status": "publishing", "approved_at": now, "approved_by": admin_id}
            if approve
            else {"status": "rejected", "rejected_at": now, "rejected_by": admin_id}
        )
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(WantedAd)
                .where(WantedAd.id == ad_id, WantedAd.status == "pending")
                .values(**values)
            )
            if result.rowcount == 1:
                return str(values["status"])
            current = await session.get(WantedAd, ad_id)
            if current is None:
                return "missing"
            return f"already_{current.status}"

    async def release_publication(self, ad_id: int) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(WantedAd)
                .where(WantedAd.id == ad_id, WantedAd.status == "publishing")
                .values(status="pending")
            )

    async def mark_published(self, ad_id: int, message_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            await session.execute(
                update(WantedAd)
                .where(WantedAd.id == ad_id, WantedAd.status == "publishing")
                .values(
                    status="published",
                    telegram_message_id=message_id,
                    published_at=now,
                    updated_at=now,
                )
            )

    async def pending(self, limit: int = 20) -> list[WantedAd]:
        async with self.sessions() as session:
            result = await session.execute(
                select(WantedAd)
                .where(WantedAd.status == "pending")
                .order_by(WantedAd.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars())

    async def owned(self, user_id: int, limit: int = 10) -> list[WantedAd]:
        async with self.sessions() as session:
            result = await session.execute(
                select(WantedAd)
                .where(WantedAd.telegram_user_id == user_id)
                .order_by(WantedAd.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars())

    async def counts(self) -> dict[str, int]:
        async with self.sessions() as session:
            result = await session.execute(
                select(WantedAd.status, func.count()).group_by(WantedAd.status)
            )
            return {str(status): int(count) for status, count in result}

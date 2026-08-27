from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DailyFeaturedPublication, FeaturedCandidate, FeaturedReviewState

LOCK_ID = 731_205_001


class FeaturedRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @asynccontextmanager
    async def run_lock(self) -> AsyncIterator[bool]:
        """Hold one dedicated PostgreSQL connection for the complete run."""
        async with self.sessions() as session:
            if session.bind is None or session.bind.dialect.name != "postgresql":
                yield True
                return
            acquired = bool(
                await session.scalar(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID}
                    )

    async def for_date(self, business_date: date) -> list[DailyFeaturedPublication]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(DailyFeaturedPublication)
                .where(DailyFeaturedPublication.business_date == business_date)
                .order_by(DailyFeaturedPublication.slot)
            )
            return list(rows)

    async def add_candidate(
        self, business_date: date, *, apartment_id: int | None, lalafo_id: int,
        source_url: str, source_payload: dict[str, object], rank: int,
    ) -> FeaturedCandidate:
        async with self.sessions.begin() as session:
            row = await session.scalar(select(FeaturedCandidate).where(
                FeaturedCandidate.business_date == business_date,
                FeaturedCandidate.source_lalafo_id == lalafo_id,
            ))
            if row is None:
                row = FeaturedCandidate(
                    business_date=business_date, source_apartment_id=apartment_id,
                    source_lalafo_id=lalafo_id, source_url=source_url,
                    source_payload=source_payload, rank=rank,
                )
                session.add(row)
                await session.flush()
            elif rank == 0:
                # A link explicitly supplied by the admin is authoritative and
                # refreshes an earlier automatically discovered snapshot.
                row.source_url = source_url
                row.source_payload = source_payload
                row.rank = 0
            return row

    async def bind_candidate_apartment(
        self, candidate_id: int, apartment_id: int
    ) -> None:
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id)
            if candidate is None:
                raise LookupError("Featured candidate not found")
            candidate.source_apartment_id = apartment_id

    async def suggested_candidates(self, business_date: date) -> list[FeaturedCandidate]:
        async with self.sessions() as session:
            rows = await session.scalars(select(FeaturedCandidate).where(
                FeaturedCandidate.business_date == business_date
            ).order_by(FeaturedCandidate.rank, FeaturedCandidate.id))
            return list(rows)

    async def select_candidate(self, candidate_id: int, *, limit: int = 2) -> tuple[str, FeaturedCandidate | None]:
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id, with_for_update=True)
            if candidate is None:
                return "missing", None
            if candidate.status in {"selected", "approved"}:
                return "already_selected", candidate
            selected = list(await session.scalars(
                select(FeaturedCandidate).where(
                    FeaturedCandidate.business_date == candidate.business_date,
                    FeaturedCandidate.status.in_(("selected", "approved")),
                ).with_for_update()
            ))
            if len(selected) >= limit:
                return "full", candidate
            used = {item.selected_slot for item in selected}
            candidate.status = "selected"
            candidate.selected_slot = next(slot for slot in range(1, limit + 1) if slot not in used)
            return "selected", candidate

    async def select_custom_candidate(
        self, candidate_id: int, *, limit: int = 2
    ) -> tuple[str, FeaturedCandidate | None]:
        """Always reserve a slot for an admin-supplied link.

        If both slots are occupied, the last slot is replaced. Explicit admin
        choices therefore take priority over the automatic shortlist.
        """
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id, with_for_update=True)
            if candidate is None:
                return "missing", None
            if candidate.status in {"selected", "approved"}:
                return "already_selected", candidate
            selected = list(await session.scalars(
                select(FeaturedCandidate).where(
                    FeaturedCandidate.business_date == candidate.business_date,
                    FeaturedCandidate.status.in_(("selected", "approved")),
                ).order_by(FeaturedCandidate.selected_slot).with_for_update()
            ))
            if len(selected) < limit:
                used = {item.selected_slot for item in selected}
                slot = next(value for value in range(1, limit + 1) if value not in used)
                outcome = "selected"
            else:
                replaced = selected[-1]
                slot = replaced.selected_slot or limit
                replaced.status = "replaced"
                replaced.selected_slot = None
                outcome = "replaced"
            candidate.status = "selected"
            candidate.selected_slot = slot
            return outcome, candidate

    async def approve_candidate(self, candidate_id: int) -> tuple[str, FeaturedCandidate | None]:
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id, with_for_update=True)
            if candidate is None:
                return "missing", None
            if candidate.status == "approved":
                return "already_approved", candidate
            if candidate.status != "selected":
                return "not_selected", candidate
            candidate.status = "approved"
            return "approved", candidate

    async def approve_custom_candidates(self, business_date: date) -> int:
        """Approve admin-supplied links, including links queued before auto-publish."""
        async with self.sessions.begin() as session:
            rows = list(await session.scalars(
                select(FeaturedCandidate).where(
                    FeaturedCandidate.business_date == business_date,
                    FeaturedCandidate.rank == 0,
                    FeaturedCandidate.status == "selected",
                ).with_for_update()
            ))
            for row in rows:
                row.status = "approved"
            return len(rows)

    async def reject_candidate(self, candidate_id: int) -> tuple[str, FeaturedCandidate | None]:
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id, with_for_update=True)
            if candidate is None:
                return "missing", None
            if candidate.status not in {"selected", "approved"}:
                return "not_selected", candidate
            candidate.status = "rejected"
            candidate.selected_slot = None
            return "rejected", candidate

    async def selected_candidates(self, business_date: date) -> list[FeaturedCandidate]:
        async with self.sessions() as session:
            rows = await session.scalars(select(FeaturedCandidate).where(
                FeaturedCandidate.business_date == business_date,
                FeaturedCandidate.status == "approved",
            ).order_by(FeaturedCandidate.selected_slot))
            return list(rows)

    async def mark_candidate_message(self, candidate_id: int, message_id: int) -> None:
        async with self.sessions.begin() as session:
            candidate = await session.get(FeaturedCandidate, candidate_id)
            if candidate is not None:
                candidate.telegram_message_id = message_id

    async def review_cursor(self) -> int:
        async with self.sessions() as session:
            state = await session.get(FeaturedReviewState, "telegram")
            return state.last_update_id if state is not None else 0

    async def advance_review_cursor(self, update_id: int) -> None:
        async with self.sessions.begin() as session:
            state = await session.get(FeaturedReviewState, "telegram", with_for_update=True)
            if state is None:
                session.add(FeaturedReviewState(key="telegram", last_update_id=update_id))
            elif update_id > state.last_update_id:
                state.last_update_id = update_id

    async def recent_source_ids(self, business_date: date, days: int) -> set[int]:
        cutoff = business_date - timedelta(days=max(days, 0))
        async with self.sessions() as session:
            rows = await session.scalars(select(DailyFeaturedPublication.source_lalafo_id).where(
                DailyFeaturedPublication.business_date >= cutoff,
                DailyFeaturedPublication.business_date < business_date,
            ))
            return set(rows)

    async def reserve(self, business_date: date, slot: int, apartment_id: int, lalafo_id: int) -> DailyFeaturedPublication:
        async with self.sessions.begin() as session:
            existing = await session.scalar(select(DailyFeaturedPublication).where(
                DailyFeaturedPublication.business_date == business_date,
                DailyFeaturedPublication.source_lalafo_id == lalafo_id,
            ).with_for_update())
            if existing is not None:
                return existing
            rows = list(await session.scalars(select(DailyFeaturedPublication).where(
                DailyFeaturedPublication.business_date == business_date,
            ).with_for_update()))
            occupied = {row.slot for row in rows}
            if slot in occupied:
                slot = max(occupied, default=0) + 1
            existing = await session.scalar(select(DailyFeaturedPublication).where(
                DailyFeaturedPublication.business_date == business_date,
                DailyFeaturedPublication.slot == slot,
            ).with_for_update())
            if existing is not None:
                return existing
            row = DailyFeaturedPublication(
                business_date=business_date, slot=slot,
                source_apartment_id=apartment_id, source_lalafo_id=lalafo_id,
            )
            session.add(row)
            await session.flush()
            return row

    async def daily_committed_budget(self, business_date: date) -> int:
        async with self.sessions() as session:
            value = await session.scalar(select(func.coalesce(func.sum(DailyFeaturedPublication.campaign_daily_budget), 0)).where(
                DailyFeaturedPublication.business_date == business_date
            ))
            return int(value or 0)

    async def reserve_campaign_budget(
        self, row_id: int, *, amount: int, daily_limit: int
    ) -> bool:
        """Atomically reserve budget before the external payment request."""
        async with self.sessions.begin() as session:
            row = await session.get(DailyFeaturedPublication, row_id, with_for_update=True)
            if row is None:
                raise LookupError("Featured publication not found")
            if row.campaign_id or row.campaign_daily_budget > 0:
                return False
            siblings = await session.scalars(
                select(DailyFeaturedPublication)
                .where(DailyFeaturedPublication.business_date == row.business_date)
                .with_for_update()
            )
            committed = sum(item.campaign_daily_budget for item in siblings)
            if committed + amount > daily_limit:
                return False
            row.campaign_daily_budget = amount
            row.campaign_status = "payment_pending"
            return True

    async def previous_active(self, business_date: date) -> list[DailyFeaturedPublication]:
        # A run close to midnight can cross the configured business-date
        # boundary while an ad is still being published or promoted.  Never
        # let the minute worker tear down a freshly created campaign.
        safe_cutoff = datetime.now(timezone.utc) - timedelta(hours=20)
        async with self.sessions() as session:
            rows = await session.scalars(select(DailyFeaturedPublication).where(
                DailyFeaturedPublication.business_date < business_date,
                DailyFeaturedPublication.created_at <= safe_cutoff,
                DailyFeaturedPublication.deactivated_at.is_(None),
                DailyFeaturedPublication.managed_lalafo_ad_id.is_not(None),
            ))
            return list(rows)

    async def patch(self, row_id: int, **values: object) -> DailyFeaturedPublication:
        allowed = {
            "managed_lalafo_temp_id", "managed_lalafo_uploaded_photos",
            "managed_lalafo_ad_id", "managed_lalafo_ad_url", "campaign_id",
            "campaign_status", "campaign_daily_budget", "lalafo_publication_status",
            "telegram_message_id", "telegram_chat_id", "deactivated_at", "last_error",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unsupported featured fields: {sorted(unknown)}")
        async with self.sessions.begin() as session:
            row = await session.get(DailyFeaturedPublication, row_id)
            if row is None:
                raise LookupError("Featured publication not found")
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()
            await session.refresh(row)
            return row

    async def mark_repeat(self, row_id: int, message_id: int, chat_id: int, error: str | None = None) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(DailyFeaturedPublication, row_id)
            if row is None:
                return
            row.telegram_message_id = message_id
            row.telegram_chat_id = chat_id
            row.last_telegram_repeat_at = datetime.now(timezone.utc)
            row.last_error = error

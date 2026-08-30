from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupportTicket


class SupportTicketRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
        question: str,
    ) -> SupportTicket:
        async with self.sessions() as session:
            row = SupportTicket(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                question=question,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get(self, ticket_id: int) -> SupportTicket | None:
        async with self.sessions() as session:
            return await session.get(SupportTicket, ticket_id)

    async def mark_notified(self, ticket_id: int, admin_message_id: int) -> None:
        async with self.sessions() as session:
            row = await session.get(SupportTicket, ticket_id)
            if row is None:
                return
            row.admin_message_id = admin_message_id
            await session.commit()

    async def answer(
        self,
        ticket_id: int,
        *,
        text: str,
        actor_id: int,
    ) -> bool:
        async with self.sessions() as session:
            row = await session.get(SupportTicket, ticket_id)
            if row is None or row.status != "open":
                return False
            row.status = "answered"
            row.answer = text
            row.answered_at = datetime.now(timezone.utc)
            row.answered_by = actor_id
            await session.commit()
            return True

    async def open_tickets(self, *, limit: int = 20) -> list[SupportTicket]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SupportTicket)
                .where(SupportTicket.status == "open")
                .order_by(SupportTicket.created_at.asc())
                .limit(max(1, min(100, limit)))
            )
            return list(rows.all())

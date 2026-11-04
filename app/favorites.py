from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models import Apartment, SavedApartment


class FavoriteRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        bind = sessions.kw.get("bind")
        self.dialect = bind.dialect.name if bind is not None else ""

    async def save(self, user_id: int, apartment_id: int) -> bool:
        values = {"telegram_user_id": user_id, "apartment_id": apartment_id}
        insert = postgresql_insert if self.dialect == "postgresql" else sqlite_insert
        statement = insert(SavedApartment).values(**values).on_conflict_do_nothing(
            index_elements=["telegram_user_id", "apartment_id"]
        )
        async with self.sessions.begin() as session:
            result = await session.execute(statement)
            return bool(result.rowcount)

    async def remove(self, user_id: int, apartment_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.execute(
                delete(SavedApartment).where(
                    SavedApartment.telegram_user_id == user_id,
                    SavedApartment.apartment_id == apartment_id,
                )
            )
            return bool(result.rowcount)

    async def saved_ids(self, user_id: int) -> set[int]:
        async with self.sessions() as session:
            return set(
                (
                    await session.scalars(
                        select(SavedApartment.apartment_id).where(
                            SavedApartment.telegram_user_id == user_id
                        )
                    )
                ).all()
            )

    async def apartments(self, user_id: int) -> list[Apartment]:
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(SavedApartment)
                        .options(selectinload(SavedApartment.apartment))
                        .where(SavedApartment.telegram_user_id == user_id)
                        .order_by(SavedApartment.created_at.desc())
                    )
                ).all()
            )
            return [row.apartment for row in rows if row.apartment is not None]

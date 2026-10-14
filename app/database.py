from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.engine import make_url

from app.models import Base


def normalize_database_url(url: str) -> str:
    parsed = make_url(url)
    drivers = {"sqlite": "sqlite+aiosqlite", "sqlite+aiosqlite": "sqlite+aiosqlite",
               "postgres": "postgresql+asyncpg", "postgresql": "postgresql+asyncpg",
               "postgresql+asyncpg": "postgresql+asyncpg"}
    if parsed.drivername not in drivers:
        raise ValueError("DATABASE_URL must use sqlite:// or postgresql://")
    parsed = parsed.set(drivername=drivers[parsed.drivername])
    if parsed.drivername == "postgresql+asyncpg":
        query = dict(parsed.query)
        sslmode = query.pop("sslmode", None)
        if sslmode is not None:
            query.setdefault("ssl", sslmode)
        # asyncpg does not accept libpq's channel_binding option.
        query.pop("channel_binding", None)
        parsed = parsed.set(query=query)
    return parsed.render_as_string(hide_password=False)


def create_engine_and_session(
    database_url: str, *, echo: bool = False
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = normalize_database_url(database_url)
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    if url.startswith("sqlite+"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_async_engine(url, **kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

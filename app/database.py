from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base


def normalize_database_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite://") or url.startswith("postgresql+asyncpg://"):
        normalized = url
    elif url.startswith("sqlite://"):
        normalized = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    elif url.startswith("postgres://"):
        normalized = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        normalized = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        raise ValueError("DATABASE_URL must use sqlite:// or postgresql://")
    if normalized.startswith("postgresql+asyncpg://"):
        normalized = normalized.replace("sslmode=require", "ssl=require")
        normalized = normalized.replace("&channel_binding=require", "")
        normalized = normalized.replace("?channel_binding=require&", "?")
        normalized = normalized.replace("?channel_binding=require", "")
    return normalized


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

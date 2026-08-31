from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lalafo.chat_protocol import AutoReplyJob, MessageMeta
from app.models import (
    LalafoAutoReplyCursor,
    LalafoAutoReplyJob,
    LalafoAutoReplyMeta,
)


@dataclass(frozen=True, slots=True)
class AutoReplyCursor:
    chat_key: str
    message_id: str
    message_time: int
    updated_at: int


def make_inbound_key(chat_key: str, inbound_id: str) -> str:
    value = f"{chat_key}|{inbound_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def deterministic_ack(inbound_key: str) -> str:
    digest = bytearray(
        hashlib.sha256(f"lalafo-autoreply:v1:{inbound_key}".encode("utf-8")).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _position(message_id: str, message_time: int) -> tuple[int, int, str]:
    if message_id.isdigit():
        return message_time, int(message_id), ""
    return message_time, 0, message_id


def _to_job(row: LalafoAutoReplyJob) -> AutoReplyJob:
    return AutoReplyJob(
        inbound_key=row.inbound_key,
        chat_key=row.chat_key,
        inbound_id=row.inbound_id,
        inbound_time=row.inbound_time,
        ack=row.ack,
        source=row.source,
        status=row.status,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        needs_reconcile=row.needs_reconcile,
        first_attempt_at=row.first_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class AutoReplyStore:
    """Durable, content-free state for exactly-once reply scheduling."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        bind = sessions.kw.get("bind")
        self._dialect = bind.dialect.name if bind is not None else ""

    async def initialize(self, now_ms: int) -> str:
        await self._set_if_missing("baseline_time", str(now_ms // 1000))
        fingerprint = await self.get_meta("device_fingerprint")
        if not fingerprint:
            candidate = uuid.uuid4().hex
            await self._set_if_missing("device_fingerprint", candidate)
            fingerprint = await self.get_meta("device_fingerprint") or candidate
        await self.recover_interrupted(now_ms)
        return fingerprint

    async def get_meta(self, key: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(LalafoAutoReplyMeta, key)
            return row.value if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(LalafoAutoReplyMeta, key)
            if row:
                row.value = value
            else:
                session.add(LalafoAutoReplyMeta(key=key, value=value))

    async def _set_if_missing(self, key: str, value: str) -> None:
        values = {"key": key, "value": value}
        async with self._sessions() as session, session.begin():
            if self._dialect == "postgresql":
                statement = postgresql_insert(LalafoAutoReplyMeta).values(**values)
                statement = statement.on_conflict_do_nothing(index_elements=["key"])
            elif self._dialect == "sqlite":
                statement = sqlite_insert(LalafoAutoReplyMeta).values(**values)
                statement = statement.on_conflict_do_nothing(index_elements=["key"])
            else:
                if await session.get(LalafoAutoReplyMeta, key):
                    return
                session.add(LalafoAutoReplyMeta(**values))
                return
            await session.execute(statement)

    async def has_completed_initial_sync(self) -> bool:
        return await self.get_meta("initial_sync_completed") == "1"

    async def mark_initial_sync_completed(self, now_ms: int) -> None:
        await self.set_meta("initial_sync_completed", "1")
        await self.set_meta("baseline_time", str(now_ms // 1000))

    async def baseline_time(self) -> int:
        value = await self.get_meta("baseline_time")
        try:
            return int(value or "0")
        except ValueError:
            return 0

    async def enqueue(
        self,
        chat_key: str,
        message: MessageMeta,
        source: str,
        now_ms: int,
    ) -> bool:
        inbound_key = make_inbound_key(chat_key, message.id)
        values = {
            "inbound_key": inbound_key,
            "chat_key": chat_key,
            "inbound_id": message.id,
            "inbound_time": message.created_at,
            "ack": deterministic_ack(inbound_key),
            "source": source,
            "status": "queued",
            "attempts": 0,
            "next_attempt_at": 0,
            "needs_reconcile": False,
            "first_attempt_at": None,
            "created_at": now_ms,
            "updated_at": now_ms,
        }
        inserted = False
        async with self._sessions() as session, session.begin():
            if self._dialect == "postgresql":
                statement = postgresql_insert(LalafoAutoReplyJob).values(**values)
                statement = statement.on_conflict_do_nothing(index_elements=["inbound_key"])
                result = await session.execute(statement)
                inserted = result.rowcount == 1
            elif self._dialect == "sqlite":
                statement = sqlite_insert(LalafoAutoReplyJob).values(**values)
                statement = statement.on_conflict_do_nothing(index_elements=["inbound_key"])
                result = await session.execute(statement)
                inserted = result.rowcount == 1
            elif await session.get(LalafoAutoReplyJob, inbound_key) is None:
                session.add(LalafoAutoReplyJob(**values))
                inserted = True

            if not inserted and source == "live":
                await session.execute(
                    update(LalafoAutoReplyJob)
                    .where(
                        LalafoAutoReplyJob.inbound_key == inbound_key,
                        LalafoAutoReplyJob.source == "backlog",
                        LalafoAutoReplyJob.status.in_(("queued", "retry_wait")),
                    )
                    .values(source="live", updated_at=now_ms)
                )
        return inserted

    async def recover_interrupted(self, now_ms: int) -> int:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(LalafoAutoReplyJob)
                .where(LalafoAutoReplyJob.status == "sending")
                .values(
                    status="retry_wait",
                    next_attempt_at=now_ms,
                    needs_reconcile=True,
                    updated_at=now_ms,
                )
            )
            return int(result.rowcount or 0)

    async def get_cursor(self, chat_key: str) -> AutoReplyCursor | None:
        async with self._sessions() as session:
            row = await session.get(LalafoAutoReplyCursor, chat_key)
            return (
                AutoReplyCursor(row.chat_key, row.message_id, row.message_time, row.updated_at)
                if row
                else None
            )

    async def advance_cursor(
        self, chat_key: str, message: MessageMeta, now_ms: int
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(LalafoAutoReplyCursor, chat_key)
            if row and _position(message.id, message.created_at) <= _position(
                row.message_id, row.message_time
            ):
                return
            if row:
                row.message_id = message.id
                row.message_time = message.created_at
                row.updated_at = now_ms
            else:
                session.add(
                    LalafoAutoReplyCursor(
                        chat_key=chat_key,
                        message_id=message.id,
                        message_time=message.created_at,
                        updated_at=now_ms,
                    )
                )

    async def list_ready_heads(self, now_ms: int, limit: int = 30) -> list[AutoReplyJob]:
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LalafoAutoReplyJob).where(
                            LalafoAutoReplyJob.status.in_(
                                ("queued", "sending", "retry_wait")
                            )
                        )
                    )
                ).all()
            )
        heads: dict[str, LalafoAutoReplyJob] = {}
        for row in rows:
            existing = heads.get(row.chat_key)
            row_order = (row.inbound_time, row.created_at, row.inbound_key)
            if existing is None or row_order < (
                existing.inbound_time,
                existing.created_at,
                existing.inbound_key,
            ):
                heads[row.chat_key] = row
        ready = [
            row
            for row in heads.values()
            if row.status in {"queued", "retry_wait"} and row.next_attempt_at <= now_ms
        ]
        ready.sort(
            key=lambda row: (
                0 if row.source == "live" else 1,
                row.next_attempt_at,
                row.inbound_time,
                row.created_at,
            )
        )
        return [_to_job(row) for row in ready[:limit]]

    async def claim(self, inbound_key: str, now_ms: int) -> AutoReplyJob | None:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(LalafoAutoReplyJob)
                .where(
                    LalafoAutoReplyJob.inbound_key == inbound_key,
                    LalafoAutoReplyJob.status.in_(("queued", "retry_wait")),
                )
                .values(
                    status="sending",
                    attempts=LalafoAutoReplyJob.attempts + 1,
                    first_attempt_at=case(
                        (
                            LalafoAutoReplyJob.first_attempt_at.is_(None),
                            now_ms,
                        ),
                        else_=LalafoAutoReplyJob.first_attempt_at,
                    ),
                    updated_at=now_ms,
                )
            )
            if result.rowcount != 1:
                return None
        return await self.get_job(inbound_key)

    async def get_job(self, inbound_key: str) -> AutoReplyJob | None:
        async with self._sessions() as session:
            row = await session.get(LalafoAutoReplyJob, inbound_key)
            return _to_job(row) if row else None

    async def list_jobs(self) -> list[AutoReplyJob]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(LalafoAutoReplyJob).order_by(
                        LalafoAutoReplyJob.inbound_time,
                        LalafoAutoReplyJob.created_at,
                        LalafoAutoReplyJob.inbound_key,
                    )
                )
            ).all()
            return [_to_job(row) for row in rows]

    async def mark_sent(self, inbound_key: str, now_ms: int) -> None:
        await self._update_job(
            inbound_key,
            status="sent",
            next_attempt_at=0,
            needs_reconcile=False,
            updated_at=now_ms,
        )

    async def mark_retry(
        self,
        inbound_key: str,
        next_attempt_at: int,
        needs_reconcile: bool,
        now_ms: int,
    ) -> None:
        await self._update_job(
            inbound_key,
            status="retry_wait",
            next_attempt_at=next_attempt_at,
            needs_reconcile=needs_reconcile,
            updated_at=now_ms,
        )

    async def mark_failed(self, inbound_key: str, now_ms: int) -> None:
        await self._update_job(
            inbound_key,
            status="failed",
            next_attempt_at=0,
            updated_at=now_ms,
        )

    async def _update_job(self, inbound_key: str, **values: object) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(LalafoAutoReplyJob)
                .where(LalafoAutoReplyJob.inbound_key == inbound_key)
                .values(**values)
            )

    async def queue_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("queued", "sending", "retry_wait", "sent", "failed")}
        async with self._sessions() as session:
            result = await session.execute(
                select(LalafoAutoReplyJob.status, func.count()).group_by(
                    LalafoAutoReplyJob.status
                )
            )
            for status, count in result:
                counts[str(status)] = int(count)
        return counts

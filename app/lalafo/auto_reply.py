from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from functools import cmp_to_key
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import socketio
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import create_engine_and_session
from app.lalafo.auto_reply_store import AutoReplyStore
from app.lalafo.chat_protocol import (
    AUTO_REPLY_TEXT,
    LALAFO_SOCKET_ORIGIN,
    LALAFO_SOCKET_PATH,
    MESSAGE_PAGE_SIZE,
    AutoReplyJob,
    ChatRef,
    ChatSnapshot,
    LalafoChatClient,
    LalafoGatewayError,
    LiveMessage,
    MessageMeta,
    compare_message_position,
    is_eligible_incoming,
    normalize_live_message,
    socket_connection_id,
)

logger = logging.getLogger(__name__)

MAX_PARALLEL_CHATS = 3
MAX_SEND_ATTEMPTS = 5
BACKLOG_INTERVAL_SECONDS = 1.0
RETRY_DELAYS_SECONDS = (2.0, 5.0, 15.0, 30.0, 60.0)
SOCKET_HANDSHAKE_TIMEOUT_SECONDS = 20.0


class AutoReplySynchronizer:
    def __init__(
        self,
        *,
        gateway: LalafoChatClient,
        store: AutoReplyStore,
        on_enqueued: Callable[[], None],
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._on_enqueued = on_enqueued
        self._owner_id: str | None = None
        self._buffering = True
        self._buffer: list[LiveMessage] = []
        self._raw_buffer: list[object] = []
        self._sync_lock = asyncio.Lock()
        self._policies: dict[str, tuple[bool, bool]] = {}

    async def initialize(self) -> None:
        self._owner_id = await self._gateway.get_owner_id()
        pending = self._raw_buffer
        self._raw_buffer = []
        for raw in pending:
            await self.handle_live(raw)

    def begin_buffering(self) -> None:
        self._buffering = True

    async def handle_live(self, raw: object) -> bool:
        if self._owner_id is None:
            if self._buffering:
                self._raw_buffer.append(raw)
            return self._buffering
        normalized = normalize_live_message(raw, self._owner_id)
        if normalized is None:
            return False
        cached = self._policies.get(normalized.chat.chat_key)
        event = replace(
            normalized,
            message=replace(normalized.message, payload=None),
            can_send_text=cached[0] if cached else normalized.can_send_text,
            is_blocked=cached[1] if cached else normalized.is_blocked,
        )
        if not is_eligible_incoming(
            event.message,
            self._owner_id,
            can_send_text=event.can_send_text,
            is_blocked=event.is_blocked,
        ):
            return False
        if self._buffering:
            self._buffer.append(event)
            return True
        return await self._enqueue_live(event)

    async def sync_after_connection(self) -> dict[str, int | bool]:
        async with self._sync_lock:
            if self._owner_id is None:
                await self.initialize()
            initial = not await self._store.has_completed_initial_sync()
            chats, queued = (
                await self._initial_sync() if initial else await self._incremental_sync()
            )
            buffered = self._buffer
            self._buffer = []
            for event in buffered:
                await self._enqueue_live(event)
            self._buffering = False
            return {
                "chats": chats,
                "queued": queued,
                "buffered": len(buffered),
                "initial": initial,
            }

    async def _initial_sync(self) -> tuple[int, int]:
        owner_id = self._owner_id
        assert owner_id is not None
        chats = await self._gateway.list_chats()
        queued = 0
        for chat in chats:
            self._remember_policy(chat)
            messages: list[MessageMeta] = []
            if chat.unread_count > 0:
                messages = await self._retrieve_enough_unread(
                    chat, chat.unread_count, owner_id
                )
                eligible = sorted(
                    (
                        message
                        for message in messages
                        if is_eligible_incoming(
                            message,
                            owner_id,
                            can_send_text=chat.can_send_text,
                            is_blocked=chat.is_blocked,
                        )
                    ),
                    key=cmp_to_key(compare_message_position),
                )
                after_seen = (
                    [message for message in eligible if message.created_at > chat.seen_at]
                    if chat.seen_at > 0
                    else []
                )
                selected: dict[str, MessageMeta] = {
                    message.id: message for message in after_seen[-chat.unread_count :]
                }
                if len(selected) < chat.unread_count:
                    for message in reversed(eligible):
                        selected[message.id] = message
                        if len(selected) >= chat.unread_count:
                            break
                for message in sorted(
                    selected.values(), key=cmp_to_key(compare_message_position)
                ):
                    if await self._store.enqueue(
                        chat.chat_key,
                        replace(message, payload=None),
                        "backlog",
                        _now_ms(),
                    ):
                        queued += 1
                        self._on_enqueued()

            if not messages and chat.bottom is None:
                messages = await self._gateway.retrieve_messages(chat)
            await self._advance_to_newest(chat, messages)
        await self._store.mark_initial_sync_completed(_now_ms())
        return len(chats), queued

    async def _incremental_sync(self) -> tuple[int, int]:
        owner_id = self._owner_id
        assert owner_id is not None
        baseline_time = await self._store.baseline_time()
        chats = await self._gateway.list_chats()
        queued = 0
        for chat in chats:
            self._remember_policy(chat)
            cursor = await self._store.get_cursor(chat.chat_key)
            if cursor and chat.bottom:
                cursor_message = MessageMeta(
                    id=cursor.message_id,
                    origin_id="",
                    recipient_id="",
                    type=0,
                    kind=0,
                    created_at=cursor.message_time,
                    deleted=False,
                )
                if compare_message_position(chat.bottom, cursor_message) <= 0:
                    continue
            cutoff = cursor.message_time if cursor else baseline_time
            messages = await self._retrieve_since(
                chat, cursor.message_id if cursor else None, cutoff
            )
            candidates = []
            for message in messages:
                if cursor and message.id == cursor.message_id:
                    continue
                if cursor and message.created_at < cursor.message_time:
                    continue
                if not cursor and message.created_at < baseline_time:
                    continue
                if is_eligible_incoming(
                    message,
                    owner_id,
                    can_send_text=chat.can_send_text,
                    is_blocked=chat.is_blocked,
                ):
                    candidates.append(message)
            for message in sorted(
                candidates, key=cmp_to_key(compare_message_position)
            ):
                if await self._store.enqueue(
                    chat.chat_key,
                    replace(message, payload=None),
                    "backlog",
                    _now_ms(),
                ):
                    queued += 1
                    self._on_enqueued()
            await self._advance_to_newest(chat, messages)
        return len(chats), queued

    async def _retrieve_enough_unread(
        self, chat: ChatRef, count: int, owner_id: str
    ) -> list[MessageMeta]:
        def enough(messages: list[MessageMeta]) -> bool:
            return sum(is_eligible_incoming(message, owner_id) for message in messages) >= count

        return await self._retrieve_pages(chat, enough)

    async def _retrieve_since(
        self, chat: ChatRef, cursor_id: str | None, cutoff_time: int
    ) -> list[MessageMeta]:
        def reached_cursor(messages: list[MessageMeta]) -> bool:
            if cursor_id and any(message.id == cursor_id for message in messages):
                return True
            return any(message.created_at < cutoff_time for message in messages)

        return await self._retrieve_pages(chat, reached_cursor)

    async def _retrieve_pages(
        self,
        chat: ChatRef,
        should_stop: Callable[[list[MessageMeta]], bool],
    ) -> list[MessageMeta]:
        collected: dict[str, MessageMeta] = {}
        start_id: str | None = None
        for _ in range(200):
            page = await self._gateway.retrieve_messages(chat, start_id)
            for message in page:
                collected[message.id] = message
            all_messages = list(collected.values())
            if should_stop(all_messages) or len(page) < MESSAGE_PAGE_SIZE:
                break
            oldest = sorted(page, key=cmp_to_key(compare_message_position))[0]
            if oldest.id == start_id:
                break
            start_id = oldest.id
        return list(collected.values())

    async def _enqueue_live(self, event: LiveMessage) -> bool:
        inserted = await self._store.enqueue(
            event.chat.chat_key, event.message, "live", _now_ms()
        )
        await self._store.advance_cursor(event.chat.chat_key, event.message, _now_ms())
        if inserted:
            self._on_enqueued()
        return inserted

    def _remember_policy(self, chat: ChatSnapshot) -> None:
        self._policies[chat.chat_key] = (chat.can_send_text, chat.is_blocked)

    async def _advance_to_newest(
        self, chat: ChatSnapshot, messages: list[MessageMeta]
    ) -> None:
        candidates = [*messages, *([chat.bottom] if chat.bottom else [])]
        if not candidates:
            return
        newest = sorted(candidates, key=cmp_to_key(compare_message_position))[-1]
        await self._store.advance_cursor(chat.chat_key, newest, _now_ms())


class AutoReplyScheduler:
    def __init__(
        self,
        *,
        store: AutoReplyStore,
        gateway: LalafoChatClient,
        on_sent: Callable[[], Awaitable[None]],
        on_state_changed: Callable[[], Awaitable[None]],
        on_halted: Callable[[str], None],
        on_offline: Callable[[], None],
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._on_sent = on_sent
        self._on_state_changed = on_state_changed
        self._on_halted = on_halted
        self._on_offline = on_offline
        self._active_chats: set[str] = set()
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._online = False
        self._halted = False
        self._wake = asyncio.Event()
        self._backlog_lock = asyncio.Lock()
        self._next_backlog_at = 0.0

    @property
    def halted(self) -> bool:
        return self._halted

    async def initialize(self) -> None:
        self._halted = await self._store.get_meta("sending_halted") == "1"

    def set_online(self, online: bool) -> None:
        self._online = online
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def start(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.pump()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.1)
            except TimeoutError:
                pass
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def pump(self) -> None:
        if not self._online or self._halted:
            return
        available = MAX_PARALLEL_CHATS - len(self._active_chats)
        if available <= 0:
            return
        candidates = await self._store.list_ready_heads(_now_ms())
        launched = 0
        for candidate in candidates:
            if launched >= available:
                break
            if candidate.chat_key in self._active_chats:
                continue
            claimed = await self._store.claim(candidate.inbound_key, _now_ms())
            if claimed is None:
                continue
            launched += 1
            self._active_chats.add(claimed.chat_key)
            task = asyncio.create_task(self._run_job(claimed))
            self._active_tasks.add(task)
            task.add_done_callback(
                lambda finished, chat_key=claimed.chat_key: self._job_finished(
                    finished, chat_key
                )
            )

    def _job_finished(self, task: asyncio.Task[None], chat_key: str) -> None:
        self._active_tasks.discard(task)
        self._active_chats.discard(chat_key)
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        self.wake()

    async def _run_job(self, job: AutoReplyJob) -> None:
        try:
            if job.needs_reconcile and await self._gateway.reconcile_reply(job):
                await self._store.mark_sent(job.inbound_key, _now_ms())
                await self._on_sent()
                return
            if job.source == "backlog":
                await self._wait_for_backlog_slot()
            await self._gateway.send_reply(job)
            await self._store.mark_sent(job.inbound_key, _now_ms())
            await self._on_sent()
        except Exception as exc:
            await self._handle_failure(job, exc)

    async def _wait_for_backlog_slot(self) -> None:
        async with self._backlog_lock:
            delay = max(0.0, self._next_backlog_at - time.monotonic())
            if delay:
                await asyncio.sleep(delay)
            self._next_backlog_at = time.monotonic() + BACKLOG_INTERVAL_SECONDS

    async def _handle_failure(self, job: AutoReplyJob, error: Exception) -> None:
        now_ms = _now_ms()
        kind = error.kind if isinstance(error, LalafoGatewayError) else "unexpected"
        if isinstance(error, LalafoGatewayError) and error.kind == "security_challenge":
            await self._store.mark_retry(job.inbound_key, now_ms + 60_000, True, now_ms)
            await self._store.set_meta("sending_halted", "1")
            self._halted = True
            self._on_halted("security_challenge")
            await self._on_state_changed()
            logger.error("Lalafo auto-reply sending halted: security challenge")
            return
        if isinstance(error, LalafoGatewayError) and error.kind == "blocked":
            await self._store.mark_failed(job.inbound_key, now_ms)
            await self._on_state_changed()
            return
        current = await self._store.get_job(job.inbound_key) or job
        if current.attempts >= MAX_SEND_ATTEMPTS or (
            isinstance(error, LalafoGatewayError)
            and error.kind in {"permanent", "protocol"}
        ):
            await self._store.mark_failed(job.inbound_key, now_ms)
            await self._on_state_changed()
            logger.error(
                "Lalafo auto-reply permanently failed after %d attempts (%s)",
                current.attempts,
                kind,
            )
            return
        if (
            isinstance(error, LalafoGatewayError)
            and error.kind == "rate_limit"
            and error.retry_after_ms is not None
        ):
            delay_ms = max(0, error.retry_after_ms)
        else:
            base = RETRY_DELAYS_SECONDS[
                min(max(current.attempts - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
            ]
            delay_ms = round(base * random.uniform(0.85, 1.15) * 1000)
        needs_reconcile = current.needs_reconcile or (
            isinstance(error, LalafoGatewayError) and error.ambiguous
        )
        await self._store.mark_retry(
            job.inbound_key, now_ms + delay_ms, needs_reconcile, now_ms
        )
        await self._on_state_changed()
        if isinstance(error, LalafoGatewayError) and error.kind in {"offline", "auth"}:
            self._on_offline()


class LalafoAutoResponder:
    def __init__(
        self,
        *,
        login: str,
        password: str,
        database_url: str = "sqlite:///data/bot.db",
        poll_seconds: float | None = None,
        client: LalafoChatClient | None = None,
        socket: socketio.AsyncClient | None = None,
        store: AutoReplyStore | None = None,
    ) -> None:
        del poll_seconds
        self._login = login
        self._password = password
        self._engine: AsyncEngine | None = None
        if store is None:
            self._engine, sessions = create_engine_and_session(database_url)
            store = AutoReplyStore(sessions)
        self._store = store
        self._client = client
        self._socket = socket or socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )
        self._socket.on("message", self._on_socket_message)
        self._socket.on("SocketConnection", self._on_socket_connection)
        self._socket.on("MessageReceived", self._on_message_received)
        self._socket.on("disconnect", self._on_disconnect)

        self._stop = asyncio.Event()
        self._socket_ready = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._synchronizer: AutoReplySynchronizer | None = None
        self._scheduler: AutoReplyScheduler | None = None

        self.running = False
        self.authenticated = False
        self.websocket_connected = False
        self.last_error: str | None = None
        self.last_scan_at: datetime | None = None
        self.started_at: datetime | None = None
        self.consecutive_failures = 0
        self.reply_count = 0
        self._queue_counts = {
            "queued": 0,
            "sending": 0,
            "retry_wait": 0,
            "sent": 0,
            "failed": 0,
        }
        self._heartbeat_at: datetime | None = None

    @property
    def task_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.task_running:
            self.started_at = datetime.now(UTC)
            self._heartbeat()
            self._task = asyncio.create_task(self._supervise())

    async def close(self) -> None:
        self._stop.set()
        self._disconnected.set()
        if self._scheduler:
            self._scheduler.set_online(False)
        with suppress(Exception):
            await self._socket.disconnect()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        scheduler_task = self._scheduler_task
        if scheduler_task is not None and not scheduler_task.done():
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        self._scheduler_task = None
        if self._client:
            await self._client.close()
        if self._engine:
            await self._engine.dispose()
        self.running = False
        self.websocket_connected = False

    def status(self, *, stale_after_seconds: float = 180.0) -> dict[str, Any]:
        healthy = self.is_healthy(stale_after_seconds=stale_after_seconds)
        halted = self._scheduler.halted if self._scheduler else False
        return {
            "state": (
                "halted"
                if halted
                else "running"
                if self.running and healthy
                else "recovering"
                if self.task_running
                else "stopped"
            ),
            "task_running": self.task_running,
            "authenticated": self.authenticated,
            "websocket_connected": self.websocket_connected,
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "reply_count": self.reply_count,
            "queue": dict(self._queue_counts),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    def is_healthy(self, *, stale_after_seconds: float = 180.0) -> bool:
        if not self.task_running:
            return False
        reference = self._heartbeat_at or self.started_at
        if reference is None:
            return False
        return (datetime.now(UTC) - reference).total_seconds() <= max(
            30.0, stale_after_seconds
        )

    async def _initialize(self) -> None:
        fingerprint = await self._store.initialize(_now_ms())
        if self._client is None:
            self._client = LalafoChatClient(
                login=self._login,
                password=self._password,
                fingerprint=fingerprint,
            )
        self._synchronizer = AutoReplySynchronizer(
            gateway=self._client,
            store=self._store,
            on_enqueued=self._wake_scheduler,
        )
        self._scheduler = AutoReplyScheduler(
            store=self._store,
            gateway=self._client,
            on_sent=self._on_sent,
            on_state_changed=self._refresh_counts,
            on_halted=self._on_halted,
            on_offline=self._force_reconnect,
        )
        await self._scheduler.initialize()
        await self._refresh_counts()
        self._scheduler_task = asyncio.create_task(self._scheduler.start(self._stop))

    async def _supervise(self) -> None:
        await self._initialize()
        assert self._client is not None
        assert self._synchronizer is not None
        assert self._scheduler is not None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._synchronizer.begin_buffering()
                self._socket_ready.clear()
                self._disconnected.clear()
                self._scheduler.set_online(False)
                session = await self._client.get_session()
                self.authenticated = True
                query = urlencode({"token": session.access_token})
                await self._socket.connect(
                    f"{LALAFO_SOCKET_ORIGIN}?{query}",
                    socketio_path=LALAFO_SOCKET_PATH,
                    transports=["websocket"],
                    wait_timeout=20,
                )
                await asyncio.wait_for(
                    self._socket_ready.wait(), timeout=SOCKET_HANDSHAKE_TIMEOUT_SECONDS
                )
                self.websocket_connected = True
                await self._synchronizer.initialize()
                stats = await self._synchronizer.sync_after_connection()
                self.last_scan_at = datetime.now(UTC)
                await self._refresh_counts()
                self._scheduler.set_online(True)
                self.running = True
                self.last_error = None
                self.consecutive_failures = 0
                self._heartbeat()
                logger.info(
                    "Lalafo auto-reply synchronized (%d chats, %d queued, initial=%s)",
                    stats["chats"],
                    stats["queued"],
                    stats["initial"],
                )
                backoff = 1.0
                await self._wait_until_disconnected()
            except asyncio.CancelledError:
                raise
            except LalafoGatewayError as exc:
                self.last_error = exc.kind
                self.consecutive_failures += 1
                if exc.kind in {"auth", "security_challenge"}:
                    self._client.invalidate_session()
                    self.authenticated = False
                logger.warning("Lalafo auto-reply connection failed (%s)", exc.kind)
                if exc.kind == "security_challenge":
                    # A 403/CAPTCHA is an explicit stop condition. Repeated
                    # unattended logins can turn a temporary challenge into an
                    # account block, so require a fresh operator-triggered run.
                    self._stop.set()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.consecutive_failures += 1
                logger.exception("Lalafo auto-reply connection cycle failed")
            finally:
                self.running = False
                self.websocket_connected = False
                self._scheduler.set_online(False)
                self._client.set_socket_id(None)
                with suppress(Exception):
                    await self._socket.disconnect()
                self._heartbeat()
            if not self._stop.is_set():
                await self._sleep_with_heartbeat(backoff * random.uniform(0.85, 1.15))
                backoff = min(backoff * 2, 300.0)

    async def _wait_until_disconnected(self) -> None:
        while not self._stop.is_set() and not self._disconnected.is_set():
            try:
                await asyncio.wait_for(self._disconnected.wait(), timeout=30.0)
            except TimeoutError:
                self._heartbeat()

    async def _sleep_with_heartbeat(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(30.0, remaining))
            except TimeoutError:
                self._heartbeat()

    async def _on_socket_message(self, payload: object, *_: object) -> None:
        await self._handle_socket_payload(payload)

    async def _on_socket_connection(self, data: object, *_: object) -> None:
        payload = {**(data if isinstance(data, dict) else {}), "ref": "SocketConnection"}
        await self._handle_socket_payload(payload)

    async def _on_message_received(self, data: object, *_: object) -> None:
        payload = {**(data if isinstance(data, dict) else {}), "ref": "MessageReceived"}
        await self._handle_socket_payload(payload)

    async def _handle_socket_payload(self, payload: object) -> None:
        socket_id = socket_connection_id(payload)
        if socket_id:
            if self._client:
                self._client.set_socket_id(socket_id)
            self._socket_ready.set()
            return
        if self._synchronizer:
            try:
                await self._synchronizer.handle_live(payload)
            except Exception:
                self.last_error = "live_event_processing"
                logger.exception("Could not persist a Lalafo live event")

    async def _on_disconnect(self, *_: object) -> None:
        self.websocket_connected = False
        self._disconnected.set()

    def _force_reconnect(self) -> None:
        self._disconnected.set()

    def _wake_scheduler(self) -> None:
        if self._scheduler:
            self._scheduler.wake()

    async def _on_sent(self) -> None:
        self.reply_count += 1
        await self._refresh_counts()

    def _on_halted(self, reason: str) -> None:
        self.last_error = reason

    async def _refresh_counts(self) -> None:
        self._queue_counts = await self._store.queue_counts()
        self._heartbeat()

    def _heartbeat(self) -> None:
        self._heartbeat_at = datetime.now(UTC)


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "AUTO_REPLY_TEXT",
    "AutoReplyScheduler",
    "AutoReplySynchronizer",
    "LalafoAutoResponder",
    "LalafoChatClient",
    "LalafoGatewayError",
]

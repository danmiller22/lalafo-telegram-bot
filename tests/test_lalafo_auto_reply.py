from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

try:
    import socketio  # noqa: F401
except ModuleNotFoundError:
    # The checked-in test venv predates the Socket.IO dependency. Production
    # and CI install requirements.txt; these unit tests exercise no real socket.
    sys.modules["socketio"] = SimpleNamespace(AsyncClient=object)  # type: ignore[assignment]

from app.database import create_engine_and_session, init_db
from app.lalafo.auto_reply import AutoReplyScheduler, AutoReplySynchronizer
from app.lalafo.auto_reply_store import AutoReplyStore
from app.lalafo.chat_protocol import (
    AUTO_REPLY_TEXT,
    AutoReplyJob,
    ChatRef,
    ChatSnapshot,
    FeedId,
    LalafoChatClient,
    LalafoGatewayError,
    MessageMeta,
    is_eligible_incoming,
    make_chat_key,
    normalize_chat_snapshot,
    normalize_live_message,
    socket_connection_id,
)


OWNER_ID = "1000"
CUSTOMER_ID = "2000"
AD_ID = "3000"


@pytest_asyncio.fixture
async def store():
    engine, sessions = create_engine_and_session("sqlite:///:memory:")
    await init_db(engine)
    state = AutoReplyStore(sessions)
    await state.initialize(1_700_000_000_000)
    try:
        yield state
    finally:
        await engine.dispose()


def message(
    message_id: str,
    *,
    origin: str = CUSTOMER_ID,
    recipient: str = OWNER_ID,
    message_type: int = 1,
    kind: int = 1,
    created: int = 1_700_000_002,
    deleted: bool = False,
    payload: str | None = "synthetic fixture",
) -> MessageMeta:
    return MessageMeta(
        id=message_id,
        origin_id=origin,
        recipient_id=recipient,
        type=message_type,
        kind=kind,
        created_at=created,
        deleted=deleted,
        payload=payload,
    )


def chat_ref(*, customer: str = CUSTOMER_ID, ad: str = AD_ID) -> ChatRef:
    feed_id = FeedId(OWNER_ID, customer, ad)
    return ChatRef(make_chat_key(1, feed_id), 1, feed_id, customer)


def snapshot(
    *,
    unread: int,
    bottom: MessageMeta | None,
    seen: int = 1_700_000_000,
    customer: str = CUSTOMER_ID,
    ad: str = AD_ID,
) -> ChatSnapshot:
    chat = chat_ref(customer=customer, ad=ad)
    return ChatSnapshot(
        chat_key=chat.chat_key,
        feed_type=chat.feed_type,
        feed_id=chat.feed_id,
        opponent_id=chat.opponent_id,
        unread_count=unread,
        seen_at=seen,
        bottom=bottom,
    )


class FakeGateway:
    def __init__(
        self,
        chats: list[ChatSnapshot] | None = None,
        histories: dict[str, list[MessageMeta]] | None = None,
    ) -> None:
        self.chats = chats or []
        self.histories = histories or {}
        self.sent: list[AutoReplyJob] = []
        self.reconciled = False

    async def get_owner_id(self) -> str:
        return OWNER_ID

    async def list_chats(self) -> list[ChatSnapshot]:
        return self.chats

    async def retrieve_messages(
        self, chat: ChatRef, start_id: str | None = None
    ) -> list[MessageMeta]:
        del start_id
        return self.histories.get(chat.chat_key, [])

    async def send_reply(self, job: AutoReplyJob) -> None:
        self.sent.append(job)

    async def reconcile_reply(self, job: AutoReplyJob) -> bool:
        del job
        return self.reconciled


def live_payload(
    message_id: str,
    *,
    message_type: int = 1,
    kind: int = 1,
    origin: int = 2000,
    recipient: int = 1000,
    feed_type: int = 1,
) -> dict:
    feed_id = {"userId1": 1000, "userId2": 2000}
    if feed_type == 1:
        feed_id["adId"] = 3000
    return {
        "ref": "MessageReceived",
        "feedType": feed_type,
        "feedId": feed_id,
        "message": {
            "id": message_id,
            "type": message_type,
            "kind": kind,
            "origin": origin,
            "recipient": recipient,
            "created": 1_700_000_002,
            "payload": "synthetic fixture",
        },
    }


async def wait_scheduler(scheduler: AutoReplyScheduler) -> None:
    while scheduler._active_tasks:  # noqa: SLF001 - deterministic unit-test drain
        await asyncio.gather(*list(scheduler._active_tasks))  # noqa: SLF001


def callbacks() -> tuple[AsyncMock, AsyncMock, list[str], list[bool]]:
    sent = AsyncMock()
    changed = AsyncMock()
    halted: list[str] = []
    offline: list[bool] = []
    return sent, changed, halted, offline


def test_fixed_text_exactly_matches_requested_one_line_reply() -> None:
    assert AUTO_REPLY_TEXT == (
        "Здравствуйте! 👋 Квартира актуальна. Все актуальные варианты квартир собраны "
        "в нашем Telegram-канале.  🏠 Новые варианты добавляются регулярно. 📞 Там же "
        "можно получить контакт для связи.  👉 Telegram: https://t.me/arendabishkek3"
    )
    assert "\n" not in AUTO_REPLY_TEXT


@pytest.mark.parametrize(
    ("candidate", "eligible"),
    [
        (message("text"), True),
        (message("prepared", message_type=3), True),
        (message("media", kind=2), True),
        (message("own", origin=OWNER_ID, recipient=CUSTOMER_ID), False),
        (message("system", message_type=2), False),
        (message("trigger", message_type=4), False),
        (message("favourite", message_type=42), False),
        (message("deleted", deleted=True), False),
        (message("wrong-recipient", recipient="9999"), False),
    ],
)
def test_filters_only_customer_text_prepared_and_media(
    candidate: MessageMeta, eligible: bool
) -> None:
    assert is_eligible_incoming(candidate, OWNER_ID) is eligible


def test_normalizes_ad_and_user_to_user_live_events() -> None:
    ad_event = normalize_live_message(live_payload("ad"), OWNER_ID)
    direct_event = normalize_live_message(
        live_payload("direct", feed_type=3), OWNER_ID
    )
    assert ad_event is not None and ad_event.chat.feed_type == 1
    assert ad_event.chat.feed_id.ad_id == AD_ID
    assert direct_event is not None and direct_event.chat.feed_type == 3
    assert direct_event.chat.feed_id.ad_id is None


def test_normalizes_chat_snapshot_without_storing_customer_content() -> None:
    chat = normalize_chat_snapshot(
        {
            "feedType": 1,
            "opponent": {"id": 2000},
            "ad": {"id": 3000},
            "unread": 1,
            "seen": 1_700_000_000,
            "bottom": {
                "id": "incoming",
                "type": 1,
                "kind": 1,
                "origin": 2000,
                "recipient": 1000,
                "created": 1_700_000_002,
                "payload": "synthetic fixture",
            },
        },
        OWNER_ID,
    )
    assert chat is not None
    assert chat.unread_count == 1
    assert chat.bottom is not None and chat.bottom.id == "incoming"


def test_reads_only_server_issued_socket_connection_id() -> None:
    assert socket_connection_id({"ref": "SocketConnection", "socketId": "server-id"}) == (
        "server-id"
    )
    assert socket_connection_id({"ref": "MessageReceived", "id": "message-id"}) is None
    assert socket_connection_id(
        {"ref": "SocketConnection", "data": {"socketId": "nested-server-id"}}
    ) == "nested-server-id"


@pytest.mark.asyncio
async def test_store_deduplicates_each_inbound_message_id(store: AutoReplyStore) -> None:
    chat = chat_ref()
    assert await store.enqueue(chat.chat_key, message("same"), "live", 1000)
    assert not await store.enqueue(chat.chat_key, message("same"), "live", 1001)
    assert await store.enqueue(chat.chat_key, message("next"), "live", 1002)
    jobs = await store.list_jobs()
    assert [job.inbound_id for job in jobs] == ["same", "next"]
    assert jobs[0].ack != jobs[1].ack


@pytest.mark.asyncio
async def test_restart_recovers_sending_job_for_reconciliation(
    store: AutoReplyStore,
) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("interrupted"), "live", 1000)
    candidate = (await store.list_ready_heads(1000))[0]
    claimed = await store.claim(candidate.inbound_key, 1001)
    assert claimed is not None and claimed.status == "sending"
    assert await store.recover_interrupted(1002) == 1
    recovered = await store.get_job(candidate.inbound_key)
    assert recovered is not None
    assert recovered.status == "retry_wait"
    assert recovered.needs_reconcile


@pytest.mark.asyncio
async def test_initial_sync_queues_each_unread_message_but_not_read_history(
    store: AutoReplyStore,
) -> None:
    chat = snapshot(unread=2, bottom=message("new-2", created=1_700_000_003))
    history = [
        message("old", created=1_699_999_999),
        message("new-1", created=1_700_000_002),
        message("new-2", created=1_700_000_003),
    ]
    gateway = FakeGateway([chat], {chat.chat_key: history})
    synchronizer = AutoReplySynchronizer(
        gateway=gateway,  # type: ignore[arg-type]
        store=store,
        on_enqueued=lambda: None,
    )
    await synchronizer.initialize()
    stats = await synchronizer.sync_after_connection()
    jobs = await store.list_jobs()
    assert stats["initial"] is True
    assert stats["queued"] == 2
    assert [job.inbound_id for job in jobs] == ["new-1", "new-2"]


@pytest.mark.asyncio
async def test_live_events_buffered_during_initial_sync_are_not_lost(
    store: AutoReplyStore,
) -> None:
    gateway = FakeGateway()
    synchronizer = AutoReplySynchronizer(
        gateway=gateway,  # type: ignore[arg-type]
        store=store,
        on_enqueued=lambda: None,
    )
    await synchronizer.initialize()
    assert await synchronizer.handle_live(live_payload("during-sync"))
    stats = await synchronizer.sync_after_connection()
    jobs = await store.list_jobs()
    assert stats["buffered"] == 1
    assert [job.inbound_id for job in jobs] == ["during-sync"]


@pytest.mark.asyncio
async def test_incremental_sync_ignores_read_flag_and_uses_cursor(
    store: AutoReplyStore,
) -> None:
    chat = snapshot(unread=0, bottom=message("cursor", created=1_700_000_001))
    first_gateway = FakeGateway([chat], {chat.chat_key: [chat.bottom]})
    first = AutoReplySynchronizer(
        gateway=first_gateway,  # type: ignore[arg-type]
        store=store,
        on_enqueued=lambda: None,
    )
    await first.initialize()
    await first.sync_after_connection()

    newer = message("new-even-if-opened", created=1_700_000_002)
    changed_chat = snapshot(unread=0, bottom=newer)
    second_gateway = FakeGateway(
        [changed_chat], {changed_chat.chat_key: [newer, chat.bottom]}
    )
    second = AutoReplySynchronizer(
        gateway=second_gateway,  # type: ignore[arg-type]
        store=store,
        on_enqueued=lambda: None,
    )
    await second.initialize()
    stats = await second.sync_after_connection()
    jobs = await store.list_jobs()
    assert stats["initial"] is False
    assert [job.inbound_id for job in jobs] == ["new-even-if-opened"]


@pytest.mark.asyncio
async def test_duplicate_live_event_and_backlog_produce_one_job(
    store: AutoReplyStore,
) -> None:
    incoming = message("deduplicated")
    chat = snapshot(unread=1, bottom=incoming)
    gateway = FakeGateway([chat], {chat.chat_key: [incoming]})
    synchronizer = AutoReplySynchronizer(
        gateway=gateway,  # type: ignore[arg-type]
        store=store,
        on_enqueued=lambda: None,
    )
    await synchronizer.initialize()
    await synchronizer.handle_live(live_payload("deduplicated"))
    await synchronizer.sync_after_connection()
    assert len(await store.list_jobs()) == 1


@pytest.mark.asyncio
async def test_scheduler_sends_two_messages_in_same_chat_in_fifo_order(
    store: AutoReplyStore,
) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("first", created=1), "live", 1)
    await store.enqueue(chat.chat_key, message("second", created=2), "live", 2)
    gateway = FakeGateway()
    sent, changed, halted, offline = callbacks()
    scheduler = AutoReplyScheduler(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        on_sent=sent,
        on_state_changed=changed,
        on_halted=halted.append,
        on_offline=lambda: offline.append(True),
    )
    await scheduler.initialize()
    scheduler.set_online(True)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    assert [job.inbound_id for job in gateway.sent] == ["first", "second"]
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_scheduler_reconciles_interrupted_send_without_resending(
    store: AutoReplyStore,
) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("ambiguous"), "live", 1)
    head = (await store.list_ready_heads(1))[0]
    await store.claim(head.inbound_key, 2)
    await store.recover_interrupted(3)
    gateway = FakeGateway()
    gateway.reconciled = True
    sent, changed, halted, offline = callbacks()
    scheduler = AutoReplyScheduler(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        on_sent=sent,
        on_state_changed=changed,
        on_halted=halted.append,
        on_offline=lambda: offline.append(True),
    )
    await scheduler.initialize()
    scheduler.set_online(True)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    assert gateway.sent == []
    assert (await store.get_job(head.inbound_key)).status == "sent"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_ambiguous_network_failure_is_retried_with_reconciliation(
    store: AutoReplyStore,
) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("timeout"), "live", 1)
    gateway = FakeGateway()

    async def fail(_: AutoReplyJob) -> None:
        raise LalafoGatewayError("network", ambiguous=True)

    gateway.send_reply = fail  # type: ignore[method-assign]
    sent, changed, halted, offline = callbacks()
    scheduler = AutoReplyScheduler(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        on_sent=sent,
        on_state_changed=changed,
        on_halted=halted.append,
        on_offline=lambda: offline.append(True),
    )
    await scheduler.initialize()
    scheduler.set_online(True)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    job = (await store.list_jobs())[0]
    assert job.status == "retry_wait"
    assert job.needs_reconcile
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after(store: AutoReplyStore) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("limited"), "live", 1)
    gateway = FakeGateway()

    async def fail(_: AutoReplyJob) -> None:
        raise LalafoGatewayError("rate_limit", retry_after_ms=120_000)

    gateway.send_reply = fail  # type: ignore[method-assign]
    sent, changed, halted, offline = callbacks()
    scheduler = AutoReplyScheduler(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        on_sent=sent,
        on_state_changed=changed,
        on_halted=halted.append,
        on_offline=lambda: offline.append(True),
    )
    await scheduler.initialize()
    scheduler.set_online(True)
    before = int(__import__("time").time() * 1000)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    job = (await store.list_jobs())[0]
    assert job.next_attempt_at >= before + 119_000


@pytest.mark.asyncio
async def test_security_challenge_halts_all_sending(store: AutoReplyStore) -> None:
    chat = chat_ref()
    await store.enqueue(chat.chat_key, message("challenge"), "live", 1)
    gateway = FakeGateway()

    async def fail(_: AutoReplyJob) -> None:
        raise LalafoGatewayError("security_challenge", status=403)

    gateway.send_reply = fail  # type: ignore[method-assign]
    sent, changed, halted, offline = callbacks()
    scheduler = AutoReplyScheduler(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        on_sent=sent,
        on_state_changed=changed,
        on_halted=halted.append,
        on_offline=lambda: offline.append(True),
    )
    await scheduler.initialize()
    scheduler.set_online(True)
    await scheduler.pump()
    await wait_scheduler(scheduler)
    assert scheduler.halted
    assert halted == ["security_challenge"]
    assert await store.get_meta("sending_halted") == "1"


@pytest.mark.asyncio
async def test_client_uses_web_headers_server_socket_id_and_exact_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(200, text="login")
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                json={
                    "id": 1000,
                    "token": "rest-token",
                    "access_token": "socket-token",
                    "user_hash": "user-hash",
                },
            )
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(
        base_url="https://lalafo.kg", transport=httpx.MockTransport(handler)
    )
    client = LalafoChatClient(
        login="996000000000",
        password="synthetic-secret",
        fingerprint="f" * 32,
        http=http,
    )
    await client.get_session()
    client.set_socket_id("server-issued-id")
    job = AutoReplyJob(
        inbound_key="key",
        chat_key=chat_ref().chat_key,
        inbound_id="incoming",
        inbound_time=1_700_000_002,
        ack="00000000-0000-5000-8000-000000000001",
        source="live",
        status="sending",
        attempts=1,
        next_attempt_at=0,
        needs_reconcile=False,
        first_attempt_at=1,
        created_at=1,
        updated_at=1,
    )
    await client.send_reply(job)
    await http.aclose()

    preflight_request, login_request, send_request = requests
    assert preflight_request.method == "GET"
    assert preflight_request.headers["sec-fetch-mode"] == "navigate"
    assert login_request.headers["origin"] == "https://lalafo.kg"
    assert login_request.headers["referer"] == "https://lalafo.kg/login"
    assert login_request.headers["x-cache-bypass"] == "yes"
    assert login_request.headers["user-hash"]
    assert login_request.headers["device"] == "pc"
    assert login_request.headers["device-fingerprint"] == "f" * 32
    assert login_request.headers["request-id"].startswith("react-client-")
    assert send_request.headers["socket-id"] == "server-issued-id"
    body = json.loads(send_request.content)
    assert body["message"]["payload"] == AUTO_REPLY_TEXT
    assert body["ack"] == job.ack


@pytest.mark.asyncio
async def test_client_reauthenticates_once_after_401() -> None:
    calls = defaultdict(int)

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if request.url.path == "/api/auth/login":
            number = calls[request.url.path]
            return httpx.Response(
                200,
                json={
                    "id": 1000,
                    "token": f"rest-{number}",
                    "access_token": f"socket-{number}",
                    "user_hash": "hash",
                },
            )
        if calls[request.url.path] == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"chatUpdates": []})

    http = httpx.AsyncClient(
        base_url="https://lalafo.kg", transport=httpx.MockTransport(handler)
    )
    client = LalafoChatClient(
        login="996000000000",
        password="synthetic-secret",
        fingerprint="f" * 32,
        http=http,
    )
    assert await client.list_chats() == []
    await http.aclose()
    assert calls["/api/auth/login"] == 2
    assert calls["/api/chat/v4/chat-update/get-paginated"] == 2

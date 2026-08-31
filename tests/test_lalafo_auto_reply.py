from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.lalafo.auto_reply import (
    AUTO_REPLY_TEXT,
    MAX_REPLIES_PER_SCAN,
    MIN_POLL_SECONDS,
    LalafoAutoResponder,
    LalafoChatAuthenticationError,
    LalafoChatClient,
    LalafoChatRateLimitError,
    LalafoChatRejectedError,
    LalafoSession,
)


class FakeSocket:
    connected = True

    def on(self, *_: object, **__: object) -> None:
        return None

    def get_sid(self, namespace: str) -> str:
        return "socket-1"


class FakeClient:
    def __init__(self, chats: list[dict]) -> None:
        self.session = LalafoSession(100, "token", "access", "hash")
        self._chats = chats
        self.send_reply = AsyncMock()

    def require_session(self) -> LalafoSession:
        return self.session

    async def chats(self) -> list[dict]:
        return self._chats

    async def close(self) -> None:
        return None


def chat(*, message_id: int, origin: int, payload: str = "Любой вопрос") -> dict:
    return {
        "threadId": "thread-1",
        "feedType": 1,
        "ad": {"id": 99},
        "opponent": {"id": 200},
        "bottom": {
            "id": message_id,
            "origin": origin,
            "created": 1_700_000_000,
            "payload": payload,
        },
    }


@pytest.mark.asyncio
async def test_replies_once_per_thread_with_safe_fixed_text() -> None:
    client = FakeClient(
        [
            chat(message_id=1, origin=200, payload="Актуально?"),
            chat(message_id=3, origin=100, payload="Already outgoing"),
        ]
    )
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 1
    assert client.send_reply.await_count == 1
    for call in client.send_reply.await_args_list:
        assert call.args[1] == AUTO_REPLY_TEXT
        assert call.args[2] == "socket-1"


@pytest.mark.asyncio
async def test_does_not_duplicate_reply_for_same_incoming_message() -> None:
    client = FakeClient([chat(message_id=42, origin=200)])
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 1
    assert await responder.scan_once() == 0
    client.send_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limited_message_pauses_all_retries_for_an_hour() -> None:
    client = FakeClient([chat(message_id=43, origin=200)])
    client.send_reply.side_effect = [LalafoChatRateLimitError(), None]
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 0
    assert await responder.scan_once() == 0
    client.send_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanently_rejected_message_does_not_create_retry_storm() -> None:
    client = FakeClient([chat(message_id=44, origin=200)])
    client.send_reply.side_effect = LalafoChatRejectedError()
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 0
    assert await responder.scan_once() == 0
    client.send_reply.assert_awaited_once()


def test_fixed_text_matches_requested_message() -> None:
    assert AUTO_REPLY_TEXT == (
        "Здравствуйте! 👋\n"
        "Квартира актуальна.\n"
        "Все актуальные варианты квартир собраны в нашем Telegram-канале. 🏠\n"
        "Новые варианты добавляются регулярно.\n"
        "📞 Там же можно получить контакт для связи.\n"
        "👉 Telegram: https://t.me/arendabishkek3"
    )


@pytest.mark.asyncio
async def test_send_does_not_retry_a_forbidden_message() -> None:
    client = LalafoChatClient()
    client.session = LalafoSession(100, "token", "access", "hash")
    client._user_hash = "hash"
    client._http = AsyncMock()  # type: ignore[assignment]
    request = httpx.Request("POST", "https://lalafo.kg/api/chat/v4/message/send")
    client._http.post.return_value = httpx.Response(
        403, request=request, text="message rejected"
    )

    with pytest.raises(LalafoChatRejectedError):
        await client.send_reply(chat(message_id=50, origin=200), AUTO_REPLY_TEXT, "sid")

    assert client._http.post.await_count == 1
    first = client._http.post.await_args_list[0].kwargs
    assert first["json"]["message"]["payload"] == AUTO_REPLY_TEXT
    assert len(first["headers"]["device-fingerprint"]) == 32
    assert first["headers"]["Referer"] == "https://lalafo.kg/account/chats"


@pytest.mark.asyncio
async def test_thread_cooldown_blocks_a_second_new_message_in_same_chat() -> None:
    client = FakeClient([chat(message_id=61, origin=200)])
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )
    assert await responder.scan_once() == 1
    client._chats = [chat(message_id=62, origin=200)]
    assert await responder.scan_once() == 0
    client.send_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_has_a_hard_reply_batch_limit() -> None:
    chats = []
    for index in range(10):
        item = chat(message_id=100 + index, origin=200)
        item["threadId"] = f"thread-{index}"
        item["opponent"] = {"id": 200 + index}
        chats.append(item)
    client = FakeClient(chats)
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )
    assert await responder.scan_once() == MAX_REPLIES_PER_SCAN
    assert client.send_reply.await_count == MAX_REPLIES_PER_SCAN


def test_poll_interval_is_never_below_one_minute() -> None:
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        poll_seconds=1,
        client=FakeClient([]),  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )
    assert responder._poll_seconds == MIN_POLL_SECONDS


@pytest.mark.asyncio
async def test_login_retries_phone_in_browser_normalized_format() -> None:
    client = LalafoChatClient()
    client._http = AsyncMock()  # type: ignore[assignment]
    request = httpx.Request("POST", "https://lalafo.kg/api/auth/login")
    client._http.post.side_effect = [
        httpx.Response(403, request=request),
        httpx.Response(
            200,
            request=request,
            json={
                "id": 100,
                "token": "token",
                "access_token": "access",
                "user_hash": "hash",
            },
        ),
    ]

    session = await client.login("+996 600-003-060", "secret")

    assert session.profile_id == 100
    client._http.get.assert_awaited_once()
    assert client._http.post.await_count == 2
    assert client._http.post.await_args_list[0].kwargs["json"]["mobile"] == "+996 600-003-060"
    assert client._http.post.await_args_list[1].kwargs["json"]["mobile"] == "996600003060"
    assert client._http.post.await_args_list[0].kwargs["headers"]["Origin"] == (
        "https://lalafo.kg"
    )
    assert client._http.post.await_args_list[0].kwargs["headers"]["Referer"] == (
        "https://lalafo.kg/login"
    )


@pytest.mark.asyncio
async def test_login_does_not_repeat_a_forbidden_cloud_route() -> None:
    client = LalafoChatClient()
    client._http = AsyncMock()  # type: ignore[assignment]
    request = httpx.Request("POST", "https://lalafo.kg/api/auth/login")
    client._http.post.return_value = httpx.Response(403, request=request)

    with pytest.raises(LalafoChatAuthenticationError, match="HTTP 403"):
        await client.login("+996 600-003-060", "secret")

    client._http.get.assert_awaited_once()
    client._http.post.assert_awaited_once()


def test_health_requires_live_task_and_recent_progress() -> None:
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=FakeClient([]),  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )
    responder._task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    responder.started_at = datetime.now(UTC)
    assert responder.is_healthy(stale_after_seconds=180)

    responder.last_scan_at = datetime.now(UTC) - timedelta(seconds=181)
    assert not responder.is_healthy(stale_after_seconds=180)

    responder._authentication_retry_not_before = 10**20
    assert responder.is_healthy(stale_after_seconds=180)

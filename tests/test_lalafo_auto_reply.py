from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.lalafo.auto_reply import (
    AUTO_REPLY_TEXT,
    LalafoAutoResponder,
    LalafoChatRateLimitError,
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
async def test_replies_with_exact_fixed_text_to_any_incoming_message() -> None:
    client = FakeClient(
        [
            chat(message_id=1, origin=200, payload="Актуально?"),
            chat(message_id=2, origin=200, payload="Какая цена?"),
            chat(message_id=3, origin=100, payload="Already outgoing"),
        ]
    )
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 2
    assert client.send_reply.await_count == 2
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
async def test_rate_limited_message_is_retried_on_the_next_scan() -> None:
    client = FakeClient([chat(message_id=43, origin=200)])
    client.send_reply.side_effect = [LalafoChatRateLimitError(), None]
    responder = LalafoAutoResponder(
        login="ignored",
        password="ignored",
        client=client,  # type: ignore[arg-type]
        socket=FakeSocket(),  # type: ignore[arg-type]
    )

    assert await responder.scan_once() == 0
    assert await responder.scan_once() == 1
    assert client.send_reply.await_count == 2


def test_fixed_text_matches_requested_message() -> None:
    assert AUTO_REPLY_TEXT == (
        "Здравствуйте! 👋\n"
        "Квартира актуальна. Все актуальные варианты квартир собраны в нашем Telegram-канале.\n"
        "🏠 Новые варианты добавляются регулярно.\n"
        "📞 Там же можно получить контакт для связи.\n"
        "👉 Telegram:\n"
        "https://t.me/arendabishkek3"
    )

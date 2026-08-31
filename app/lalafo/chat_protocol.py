from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote, unquote

import httpx


AUTO_REPLY_TEXT = (
    "Здравствуйте! 👋 Квартира актуальна. Все актуальные варианты квартир собраны "
    "в нашем Telegram-канале.  🏠 Новые варианты добавляются регулярно. 📞 Там же "
    "можно получить контакт для связи.  👉 Telegram: https://t.me/arendabishkek3"
)

LALAFO_HTTP_ORIGIN = "https://lalafo.kg"
LALAFO_SOCKET_ORIGIN = "https://websocket.lalafo.com"
LALAFO_SOCKET_PATH = "chat-ws/socket.io"

CHAT_PAGE_SIZE = 20
MESSAGE_PAGE_SIZE = 26

AD_CHAT = 1
USER_TO_USER = 3
NORMAL_MESSAGE = 1
PREPARED_MESSAGE = 3
TEXT_MESSAGE = 1
MEDIA_MESSAGE = 2

GatewayErrorKind = Literal[
    "auth",
    "rate_limit",
    "retryable",
    "network",
    "security_challenge",
    "blocked",
    "offline",
    "permanent",
    "protocol",
]


class LalafoGatewayError(RuntimeError):
    def __init__(
        self,
        kind: GatewayErrorKind,
        *,
        status: int | None = None,
        retry_after_ms: int | None = None,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(f"Lalafo request failed ({kind})")
        self.kind = kind
        self.status = status
        self.retry_after_ms = retry_after_ms
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class LalafoSession:
    owner_id: str
    rest_token: str
    access_token: str
    user_hash: str


@dataclass(frozen=True, slots=True)
class FeedId:
    user_id_1: str
    user_id_2: str
    ad_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRef:
    chat_key: str
    feed_type: int
    feed_id: FeedId
    opponent_id: str


@dataclass(frozen=True, slots=True)
class MessageMeta:
    id: str
    origin_id: str
    recipient_id: str
    type: int
    kind: int
    created_at: int
    deleted: bool
    ack: str | None = None
    payload: str | None = None


@dataclass(frozen=True, slots=True)
class ChatSnapshot(ChatRef):
    unread_count: int = 0
    updated_at: int = 0
    seen_at: int = 0
    can_send_text: bool = True
    is_blocked: bool = False
    bottom: MessageMeta | None = None


@dataclass(frozen=True, slots=True)
class LiveMessage:
    chat: ChatRef
    message: MessageMeta
    can_send_text: bool
    is_blocked: bool


@dataclass(frozen=True, slots=True)
class AutoReplyJob:
    inbound_key: str
    chat_key: str
    inbound_id: str
    inbound_time: int
    ack: str
    source: str
    status: str
    attempts: int
    next_attempt_at: int
    needs_reconcile: bool
    first_attempt_at: int | None
    created_at: int
    updated_at: int


def _object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _first(value: dict[str, Any] | None, *keys: str) -> object | None:
    if value is None:
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate is not None:
            return candidate
    return None


def _string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _number(value: object, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return fallback


def _boolean(value: object, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True"):
        return True
    if value in (0, "0", "false", "False"):
        return False
    return fallback


def _epoch_seconds(value: object) -> int:
    numeric = _number(value)
    return numeric // 1000 if numeric > 10_000_000_000 else numeric


def normalize_feed_type(value: object) -> int | None:
    if value in (AD_CHAT, "1", "AD_CHAT", "ad_chat"):
        return AD_CHAT
    if value in (USER_TO_USER, "3", "USER_TO_USER", "user_to_user"):
        return USER_TO_USER
    return None


def make_chat_key(feed_type: int, feed_id: FeedId) -> str:
    users = sorted((feed_id.user_id_1, feed_id.user_id_2))
    parts = (str(feed_type), feed_id.ad_id or "-", users[0], users[1])
    return "|".join(quote(part, safe="") for part in parts)


def parse_chat_key(chat_key: str, owner_id: str) -> ChatRef:
    parts = [unquote(part) for part in chat_key.split("|")]
    if len(parts) != 4 or parts[0] not in {"1", "3"}:
        raise LalafoGatewayError("protocol")
    feed_type = int(parts[0])
    ad_id, first_user, second_user = parts[1:]
    if owner_id not in {first_user, second_user}:
        raise LalafoGatewayError("protocol")
    opponent_id = second_user if owner_id == first_user else first_user
    if not opponent_id or opponent_id == owner_id:
        raise LalafoGatewayError("protocol")
    return ChatRef(
        chat_key=chat_key,
        feed_type=feed_type,
        feed_id=FeedId(
            user_id_1=owner_id,
            user_id_2=opponent_id,
            ad_id=None if ad_id == "-" else ad_id,
        ),
        opponent_id=opponent_id,
    )


def compare_message_position(left: MessageMeta, right: MessageMeta) -> int:
    if left.created_at != right.created_at:
        return -1 if left.created_at < right.created_at else 1
    if left.id.isdigit() and right.id.isdigit():
        left_id, right_id = int(left.id), int(right.id)
        return -1 if left_id < right_id else 1 if left_id > right_id else 0
    return -1 if left.id < right.id else 1 if left.id > right.id else 0


def _normalize_feed_id(value: object, owner_id: str) -> FeedId | None:
    raw = _object(value)
    first_user = _string(_first(raw, "userId1", "user_id_1", "user1"))
    second_user = _string(_first(raw, "userId2", "user_id_2", "user2"))
    ad_id = _string(_first(raw, "adId", "ad_id"))
    if not first_user or not second_user or owner_id not in {first_user, second_user}:
        return None
    return FeedId(first_user, second_user, ad_id)


def _make_chat_ref(feed_type: int, feed_id: FeedId, owner_id: str) -> ChatRef | None:
    opponent_id = (
        feed_id.user_id_2 if feed_id.user_id_1 == owner_id else feed_id.user_id_1
    )
    if not opponent_id or opponent_id == owner_id:
        return None
    if feed_type == AD_CHAT and not feed_id.ad_id:
        return None
    return ChatRef(
        chat_key=make_chat_key(feed_type, feed_id),
        feed_type=feed_type,
        feed_id=feed_id,
        opponent_id=opponent_id,
    )


def normalize_message(value: object) -> MessageMeta | None:
    raw = _object(value)
    message_id = _string(_first(raw, "id", "messageId", "message_id", "_id"))
    origin_id = _string(_first(raw, "origin", "originId", "origin_id"))
    recipient_id = _string(_first(raw, "recipient", "recipientId", "recipient_id"))
    if not message_id or not origin_id or not recipient_id:
        return None
    payload = _first(raw, "payload", "text")
    deleted = _boolean(_first(raw, "is_deleted", "isDeleted", "deleted"), False)
    deleted = deleted or _first(raw, "deletedAt", "deleted_at") is not None
    return MessageMeta(
        id=message_id,
        origin_id=origin_id,
        recipient_id=recipient_id,
        type=_number(_first(raw, "type", "messageType", "message_type")),
        kind=_number(_first(raw, "kind", "messageKind", "message_kind")),
        created_at=_epoch_seconds(_first(raw, "created", "createdAt", "created_at")),
        deleted=deleted,
        ack=_string(_first(raw, "ack", "requestAck", "request_ack")),
        payload=payload if isinstance(payload, str) else None,
    )


def normalize_chat_snapshot(value: object, owner_id: str) -> ChatSnapshot | None:
    raw = _object(value)
    feed_type = normalize_feed_type(_first(raw, "feedType", "feed_type"))
    if feed_type is None:
        return None
    opponent = _object(_first(raw, "opponent", "user"))
    opponent_id = _string(_first(opponent, "id", "user_id", "userId"))
    if not opponent_id or opponent_id == owner_id:
        return None
    ad = _object(raw.get("ad") if raw else None)
    ad_id = _string(_first(ad, "id", "ad_id", "adId"))
    feed_id = _normalize_feed_id(_first(raw, "feedId", "feed_id"), owner_id)
    if feed_id is None:
        feed_id = FeedId(owner_id, opponent_id, ad_id)
    chat = _make_chat_ref(feed_type, feed_id, owner_id)
    if chat is None:
        return None
    can_send_text = _boolean(
        _first(raw, "canSendText", "can_send_text", "isTextAllowed"), True
    )
    is_blocked = _boolean(_first(raw, "isBlocked", "is_blocked", "blocked"), False)
    is_blocked = is_blocked or _boolean(
        _first(opponent, "isBlockedBySystem", "is_blocked_by_system", "blocked"),
        False,
    )
    return ChatSnapshot(
        chat_key=chat.chat_key,
        feed_type=chat.feed_type,
        feed_id=chat.feed_id,
        opponent_id=chat.opponent_id,
        unread_count=max(
            0,
            _number(_first(raw, "unread", "unreadCount", "unread_count")),
        ),
        updated_at=_epoch_seconds(_first(raw, "updated", "updatedAt", "updated_at")),
        seen_at=_epoch_seconds(_first(raw, "seen", "seenAt", "seen_at")),
        can_send_text=can_send_text,
        is_blocked=is_blocked,
        bottom=normalize_message(_first(raw, "bottom", "lastMessage", "last_message")),
    )


def normalize_live_message(value: object, owner_id: str) -> LiveMessage | None:
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
    root = _object(decoded)
    wrapper = _object(root.get("data")) if root else None
    wrapper = wrapper or root
    ref = _string(_first(wrapper, "ref", "event", "type"))
    if ref and ref != "MessageReceived":
        return None
    feed_type = normalize_feed_type(_first(wrapper, "feedType", "feed_type"))
    if feed_type is None:
        return None
    feed_id = _normalize_feed_id(_first(wrapper, "feedId", "feed_id"), owner_id)
    if feed_id is None:
        return None
    chat = _make_chat_ref(feed_type, feed_id, owner_id)
    message = normalize_message(_first(wrapper, "message", "messageEntity"))
    if chat is None or message is None:
        return None
    chat_data = _object(_first(wrapper, "chat", "chatUpdate"))
    opponent = _object(_first(chat_data, "opponent", "user"))
    can_send_text = _boolean(
        _first(chat_data, "canSendText", "can_send_text", "isTextAllowed"), True
    )
    is_blocked = _boolean(
        _first(chat_data, "isBlocked", "is_blocked", "blocked"), False
    ) or _boolean(_first(opponent, "isBlockedBySystem", "blocked"), False)
    return LiveMessage(chat, message, can_send_text, is_blocked)


def socket_connection_id(value: object) -> str | None:
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
    root = _object(decoded)
    data = _object(root.get("data")) if root else None
    event = _string(_first(data, "ref", "event", "type")) or _string(
        _first(root, "ref", "event", "type")
    )
    if event != "SocketConnection":
        return None
    return _string(_first(data, "socketId", "socket_id", "id")) or _string(
        _first(root, "socketId", "socket_id", "id")
    )


def is_eligible_incoming(
    message: MessageMeta,
    owner_id: str,
    *,
    can_send_text: bool = True,
    is_blocked: bool = False,
) -> bool:
    if not can_send_text or is_blocked or message.deleted:
        return False
    if message.origin_id == owner_id or message.recipient_id != owner_id:
        return False
    if message.type not in {NORMAL_MESSAGE, PREPARED_MESSAGE}:
        return False
    return message.kind in {TEXT_MESSAGE, MEDIA_MESSAGE}


def _find_string(root: object, keys: tuple[str, ...], depth: int = 0) -> str | None:
    if depth > 4:
        return None
    value = _object(root)
    direct = _string(_first(value, *keys))
    if direct:
        return direct
    if value:
        for key in ("data", "user", "account", "profile", "result"):
            nested = _find_string(value.get(key), keys, depth + 1)
            if nested:
                return nested
    return None


def _owner_id(root: object) -> str | None:
    value = _object(root)
    if value is None:
        return None
    for key in ("user", "account", "profile"):
        candidate = _object(value.get(key))
        found = _string(_first(candidate, "id", "user_id", "userId"))
        if found:
            return found
    data = _object(value.get("data"))
    if data:
        nested = _owner_id(data)
        if nested:
            return nested
    return _string(_first(value, "id", "user_id", "userId"))


def _extract_array(root: object, keys: tuple[str, ...]) -> list[object]:
    if isinstance(root, list):
        return root
    value = _object(root)
    if value is None:
        return []
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return _extract_array(value.get("data"), keys)


def _wire_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def _wire_feed_id(feed_id: FeedId) -> dict[str, int | str]:
    result: dict[str, int | str] = {
        "userId1": _wire_id(feed_id.user_id_1),
        "userId2": _wire_id(feed_id.user_id_2),
    }
    if feed_id.ad_id:
        result["adId"] = _wire_id(feed_id.ad_id)
    return result


def _retry_after_ms(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0, round(float(value) * 1000))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0, round((retry_at - datetime.now(UTC)).total_seconds() * 1000))
        except (TypeError, ValueError, OverflowError):
            return None


def _classify_http_failure(response: httpx.Response, body: object) -> LalafoGatewayError:
    try:
        signals = json.dumps(body, ensure_ascii=False)[:4096].lower()
    except (TypeError, ValueError):
        signals = ""
    if response.status_code in {401, 422}:
        return LalafoGatewayError("auth", status=response.status_code)
    if response.status_code == 429:
        return LalafoGatewayError(
            "rate_limit",
            status=429,
            retry_after_ms=_retry_after_ms(response),
        )
    if response.status_code == 403 or any(
        signal in signals for signal in ("captcha", "one-time", "one time", "otp", "verification code")
    ):
        return LalafoGatewayError("security_challenge", status=response.status_code)
    if any(signal in signals for signal in ("blocked", "cannot send", "can not send", "chat closed")):
        return LalafoGatewayError("blocked", status=response.status_code)
    if response.status_code >= 500:
        return LalafoGatewayError("retryable", status=response.status_code)
    return LalafoGatewayError("permanent", status=response.status_code)


class LalafoChatClient:
    """Minimal REST client matching the current first-party Lalafo chat protocol."""

    def __init__(
        self,
        *,
        login: str,
        password: str,
        fingerprint: str,
        timeout: float = 15.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._fingerprint = fingerprint
        self._http = http or httpx.AsyncClient(
            base_url=LALAFO_HTTP_ORIGIN,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
        )
        self._owns_http = http is None
        self._session: LalafoSession | None = None
        self._login_lock = asyncio.Lock()
        self._socket_id: str | None = None

    @property
    def session(self) -> LalafoSession | None:
        return self._session

    def set_socket_id(self, socket_id: str | None) -> None:
        self._socket_id = socket_id

    def invalidate_session(self) -> None:
        self._session = None
        self._socket_id = None

    async def get_session(self) -> LalafoSession:
        if self._session is not None:
            return self._session
        async with self._login_lock:
            if self._session is not None:
                return self._session
            payload = await self._raw_request(
                "/api/auth/login",
                body={
                    "email" if "@" in self._login else "mobile": self._login,
                    "password": self._password,
                },
                authenticated=False,
                mutation=True,
            )
            rest_token = _find_string(payload, ("token", "auth_token"))
            access_token = _find_string(payload, ("access_token", "accessToken")) or rest_token
            user_hash = _find_string(payload, ("user_hash", "userHash", "hash"))
            owner_id = _owner_id(payload)
            if not rest_token or not access_token or not user_hash or not owner_id:
                raise LalafoGatewayError("protocol")
            self._session = LalafoSession(owner_id, rest_token, access_token, user_hash)
            return self._session

    async def get_owner_id(self) -> str:
        return (await self.get_session()).owner_id

    async def list_chats(self) -> list[ChatSnapshot]:
        owner_id = await self.get_owner_id()
        chats: dict[str, ChatSnapshot] = {}
        skip = 0
        for _ in range(500):
            payload = await self._authorized_request(
                "/api/chat/v4/chat-update/get-paginated?sort=byNewest",
                body={
                    "syncTime": 0,
                    "feedType": [AD_CHAT, USER_TO_USER],
                    "type": [1, 2],
                    "status": [1],
                    "skip": skip,
                    "limit": CHAT_PAGE_SIZE,
                    "ref": "ChatUpdatesPaginated",
                    "ack": str(uuid.uuid4()),
                },
                mutation=True,
            )
            raw_chats = _extract_array(
                payload, ("chatUpdates", "chat_updates", "chats", "items", "result")
            )
            for raw in raw_chats:
                chat = normalize_chat_snapshot(raw, owner_id)
                if chat and chat.can_send_text and not chat.is_blocked:
                    chats[chat.chat_key] = chat
            if len(raw_chats) < CHAT_PAGE_SIZE:
                break
            skip += len(raw_chats)
        return list(chats.values())

    async def retrieve_messages(
        self, chat: ChatRef, start_id: str | None = None
    ) -> list[MessageMeta]:
        payload = await self._authorized_request(
            "/api/chat/v4/message/retrieve",
            body={
                "feedType": chat.feed_type,
                "feedId": _wire_feed_id(chat.feed_id),
                "startId": _wire_id(start_id) if start_id else 0,
                "count": MESSAGE_PAGE_SIZE,
                "direction": "up",
                "ref": "Retrieve",
                "ack": str(uuid.uuid4()),
            },
            mutation=True,
        )
        messages: dict[str, MessageMeta] = {}
        for raw in _extract_array(payload, ("messages", "items", "result")):
            message = normalize_message(raw)
            if message:
                messages[message.id] = message
        return list(messages.values())

    async def send_reply(self, job: AutoReplyJob) -> None:
        if not self._socket_id:
            raise LalafoGatewayError("offline")
        session = await self.get_session()
        chat = parse_chat_key(job.chat_key, session.owner_id)
        created = int(time.time())
        await self._authorized_request(
            "/api/chat/v4/message/send",
            body={
                "feedType": chat.feed_type,
                "feedId": _wire_feed_id(chat.feed_id),
                "message": {
                    "type": NORMAL_MESSAGE,
                    "kind": TEXT_MESSAGE,
                    "origin": _wire_id(session.owner_id),
                    "recipient": _wire_id(chat.opponent_id),
                    "created": created,
                    "payload": AUTO_REPLY_TEXT,
                    "media": [],
                    "ref": "MessageEntity",
                },
                "delivered": job.inbound_time,
                "seen": job.inbound_time,
                "ref": "Message",
                "ack": job.ack,
            },
            headers={"socket-id": self._socket_id},
            mutation=True,
            ambiguous_on_network_failure=True,
        )

    async def reconcile_reply(self, job: AutoReplyJob) -> bool:
        session = await self.get_session()
        chat = parse_chat_key(job.chat_key, session.owner_id)
        collected: dict[str, MessageMeta] = {}
        start_id: str | None = None
        found_inbound = False
        for _ in range(20):
            messages = await self.retrieve_messages(chat, start_id)
            if not messages:
                break
            for message in messages:
                collected[message.id] = message
                found_inbound = found_inbound or message.id == job.inbound_id
            if found_inbound or len(messages) < MESSAGE_PAGE_SIZE:
                break
            oldest = sorted(messages, key=lambda item: (item.created_at, item.id))[0]
            if oldest.id == start_id or oldest.created_at < job.inbound_time:
                break
            start_id = oldest.id
        ordered = sorted(collected.values(), key=lambda item: (item.created_at, item.id))
        if any(message.origin_id == session.owner_id and message.ack == job.ack for message in ordered):
            return True
        if not found_inbound:
            return False
        inbound_index = next(
            index for index, message in enumerate(ordered) if message.id == job.inbound_id
        )
        first_attempt = (job.first_attempt_at or job.created_at) // 1000 - 2
        return any(
            message.origin_id == session.owner_id
            and message.recipient_id == chat.opponent_id
            and message.payload == AUTO_REPLY_TEXT
            and message.created_at >= max(job.inbound_time, first_attempt)
            for message in ordered[inbound_index + 1 :]
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _authorized_request(
        self,
        path: str,
        *,
        body: object,
        headers: dict[str, str] | None = None,
        mutation: bool,
        ambiguous_on_network_failure: bool = False,
        retried_after_auth: bool = False,
    ) -> object:
        session = await self.get_session()
        try:
            return await self._raw_request(
                path,
                body=body,
                headers=headers,
                authenticated=True,
                session=session,
                mutation=mutation,
                ambiguous_on_network_failure=ambiguous_on_network_failure,
            )
        except LalafoGatewayError as exc:
            if exc.kind == "auth" and not retried_after_auth:
                self.invalidate_session()
                if headers and "socket-id" in headers:
                    raise LalafoGatewayError("offline") from exc
                await self.get_session()
                return await self._authorized_request(
                    path,
                    body=body,
                    headers=headers,
                    mutation=mutation,
                    ambiguous_on_network_failure=ambiguous_on_network_failure,
                    retried_after_auth=True,
                )
            raise

    async def _raw_request(
        self,
        path: str,
        *,
        body: object,
        authenticated: bool,
        mutation: bool,
        session: LalafoSession | None = None,
        headers: dict[str, str] | None = None,
        ambiguous_on_network_failure: bool = False,
    ) -> object:
        request_headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "device": "pc",
            "language": "ru_RU",
            "country-id": "12",
            "request-id": str(uuid.uuid4()),
            **(headers or {}),
        }
        if mutation:
            request_headers["device-fingerprint"] = self._fingerprint
        if authenticated:
            active = session or self._session
            if active is None:
                raise LalafoGatewayError("auth")
            request_headers["authorization"] = f"Bearer {active.rest_token}"
            request_headers["user-hash"] = active.user_hash
        try:
            response = await self._http.post(path, headers=request_headers, json=body)
        except httpx.RequestError as exc:
            raise LalafoGatewayError(
                "network", ambiguous=ambiguous_on_network_failure
            ) from exc
        try:
            payload: object = response.json() if response.content else None
        except (ValueError, json.JSONDecodeError):
            payload = None
        if response.is_error:
            raise _classify_http_failure(response, payload)
        return payload

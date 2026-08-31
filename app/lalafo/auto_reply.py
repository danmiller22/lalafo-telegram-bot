from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import socketio

logger = logging.getLogger(__name__)

AUTO_REPLY_TEXT = """Здравствуйте! 👋
Квартира актуальна.
Пожалуйста, задайте вопрос здесь, в чате Lalafo. Ответим по мере возможности."""

# Conservative product limits.  Stale cloud variables cannot restore the old
# ten-second, external-link-heavy behaviour that put the previous profile at
# unnecessary moderation risk.
MIN_POLL_SECONDS = 60.0
MAX_REPLIES_PER_SCAN = 3
MAX_REPLIES_PER_DAY = 20
THREAD_COOLDOWN_SECONDS = 24 * 60 * 60
RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60
AUTHENTICATION_COOLDOWN_SECONDS = 60 * 60
CONNECT_DEADLINE_SECONDS = 90.0
SCAN_DEADLINE_SECONDS = 90.0


class LalafoChatError(RuntimeError):
    pass


class LalafoChatAuthenticationError(LalafoChatError):
    pass


class LalafoChatRateLimitError(LalafoChatError):
    """Lalafo temporarily refused a message because of its send rate limit."""


class LalafoChatRejectedError(LalafoChatError):
    """Lalafo rejected both the full reply and its link-free fallback."""


@dataclass(slots=True)
class LalafoSession:
    profile_id: int
    token: str
    access_token: str
    user_hash: str


class LalafoChatClient:
    """Client for the authenticated chat routes used by lalafo.kg itself."""

    def __init__(self, *, timeout: float = 25.0) -> None:
        self._timeout = timeout
        self._user_hash = str(uuid.uuid4())
        # The current web client sends a 32-character browser fingerprint.
        self._fingerprint = uuid.uuid4().hex
        self._http = self._make_http()
        self.session: LalafoSession | None = None

    def _make_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://lalafo.kg",
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
        )

    def _headers(
        self, *, token: str = "", socket_id: str = "", bypass_cache: bool = False
    ) -> dict[str, str]:
        headers = {
            "device": "pc",
            "language": "ru_RU",
            "country-id": "12",
            "request-id": f"react-client-{uuid.uuid4()}",
            "Authorization": f"Bearer {token}" if token else "",
            "user-hash": self._user_hash,
            "content-type": "application/json",
            "device-fingerprint": self._fingerprint,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://lalafo.kg",
            "Referer": "https://lalafo.kg/account/chats",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": (
                '"Not=A?Brand";v="99", "Google Chrome";v="151", '
                '"Chromium";v="151"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        }
        if socket_id:
            headers["socket-id"] = socket_id
        if bypass_cache:
            headers["X-Cache-Bypass"] = "yes"
        return headers

    async def login(self, login: str, password: str) -> LalafoSession:
        field = "email" if "@" in login else "mobile"
        login_candidates = [login]
        if field == "mobile":
            # The current Lalafo form strips phone punctuation before sending the
            # login request.  Older stored credentials may still contain a leading
            # plus or spaces, so retry the exact browser representation as well.
            digits = "".join(character for character in login if character.isdigit())
            for candidate in (digits, f"+{digits}" if digits else ""):
                if candidate and candidate not in login_candidates:
                    login_candidates.append(candidate)

        async def attempt() -> httpx.Response | None:
            last_response: httpx.Response | None = None
            # Follow the same first-party flow as the website.  The initial
            # navigation establishes Lalafo's ordinary session/WAF cookies;
            # posting credentials from a completely empty cookie jar is often
            # rejected even when the account credentials are correct.
            try:
                await self._http.get(
                    "/login",
                    headers={
                        **self._headers(),
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;"
                            "q=0.9,image/avif,image/webp,*/*;q=0.8"
                        ),
                        "Referer": "https://lalafo.kg/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
            except httpx.RequestError:
                return None
            for candidate in login_candidates:
                try:
                    login_headers = self._headers()
                    login_headers["Referer"] = "https://lalafo.kg/login"
                    last_response = await self._http.post(
                        "/api/auth/login",
                        headers=login_headers,
                        json={field: candidate, "password": password},
                    )
                except httpx.RequestError:
                    return None
                # A 403 is a route/session refusal, not an alternate spelling
                # of the phone number.  Stop immediately instead of making two
                # more bot-like login attempts.  Only validation/auth failures
                # may benefit from trying the normalized browser phone value.
                if last_response.status_code == 403:
                    return last_response
                if last_response.status_code not in {401, 422}:
                    return last_response
            return last_response

        response = await attempt()
        if response is None:
            raise LalafoChatAuthenticationError("Lalafo login endpoint is unavailable")
        if response.status_code in {401, 403, 422}:
            raise LalafoChatAuthenticationError(
                f"Lalafo login rejected with HTTP {response.status_code}"
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise LalafoChatAuthenticationError("Lalafo login response is invalid")
        try:
            session = LalafoSession(
                profile_id=int(payload["id"]),
                token=str(payload["token"]),
                access_token=str(payload["access_token"]),
                user_hash=str(payload.get("user_hash") or self._user_hash),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LalafoChatAuthenticationError(
                "Lalafo login response has no required session fields"
            ) from exc
        self._user_hash = session.user_hash
        self.session = session
        return session

    def require_session(self) -> LalafoSession:
        if self.session is None:
            raise LalafoChatAuthenticationError("Lalafo session is not initialized")
        return self.session

    async def chats(self) -> list[dict[str, Any]]:
        session = self.require_session()
        response = await self._http.post(
            "/api/chat/v4/chat-update/get-paginated?sort=byNewest",
            headers=self._headers(token=session.token, bypass_cache=True),
            json={
                "syncTime": 0,
                "feedType": [1, 3],
                "type": [2],
                "status": [1],
                "skip": 0,
                "limit": 50,
                "ref": "ChatUpdatesPaginated",
                "ack": str(uuid.uuid4()),
            },
        )
        if response.status_code == 401:
            raise LalafoChatAuthenticationError("Lalafo session expired")
        if response.is_error:
            detail = response.text.replace("\n", " ")[:500]
            raise LalafoChatError(
                f"Lalafo chat list rejected with HTTP {response.status_code}: {detail}"
            )
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("chatUpdates", []) if isinstance(payload, dict) else []
        return [item for item in updates if isinstance(item, dict)]

    async def send_reply(self, chat: dict[str, Any], message: str, socket_id: str) -> None:
        session = self.require_session()
        opponent = chat.get("opponent") or {}
        opponent_id = int(opponent["id"])
        feed_type = int(chat.get("feedType") or 1)
        if feed_type == 3:
            feed_id = {"userId1": session.profile_id, "userId2": opponent_id}
        else:
            ad = chat.get("ad") or {}
            feed_id = {
                "adId": int(ad["id"]),
                "userId1": session.profile_id,
                "userId2": opponent_id,
            }
        bottom = chat.get("bottom") or {}
        created = int(time.time())
        payload = {
                "feedType": 1 if feed_type == 2 else feed_type,
                "feedId": feed_id,
                "message": {
                    "type": 1,
                    "kind": 1,
                    "origin": session.profile_id,
                    "recipient": opponent_id,
                    "created": created,
                    "payload": message,
                    "media": [],
                    "ref": "MessageEntity",
                },
                "delivered": int(bottom.get("created") or created),
                "seen": int(bottom.get("created") or created),
                "ref": "Message",
                "ack": str(uuid.uuid4()),
            }
        headers = self._headers(token=session.token, socket_id=socket_id)
        response = await self._http.post(
            "/api/chat/v4/message/send", headers=headers, json=payload
        )
        if response.status_code == 401:
            raise LalafoChatAuthenticationError("Lalafo session expired")
        if response.status_code == 429:
            raise LalafoChatRateLimitError("Lalafo message rate limit reached")
        if response.status_code == 403:
            detail = response.text.replace("\n", " ")[:300]
            raise LalafoChatRejectedError(
                f"Lalafo rejected an authenticated reply: {detail or 'HTTP 403'}"
            )
        response.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()


class LalafoAutoResponder:
    def __init__(
        self,
        *,
        login: str,
        password: str,
        poll_seconds: float = 10.0,
        client: LalafoChatClient | None = None,
        socket: socketio.AsyncClient | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._poll_seconds = max(MIN_POLL_SECONDS, poll_seconds)
        self._client = client or LalafoChatClient()
        self._socket = socket or socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            logger=False,
            engineio_logger=False,
        )
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._handled: dict[str, float] = {}
        self._handled_threads: dict[str, float] = {}
        self._reply_times: list[float] = []
        self._rate_limited_until = 0.0
        self._authentication_retry_not_before = 0.0
        self.running = False
        self.last_error: str | None = None
        self.last_scan_at: datetime | None = None
        self.started_at: datetime | None = None
        self.consecutive_failures = 0
        self.reply_count = 0
        self._socket.on("message", self._on_socket_message)
        self._socket.on("disconnect", self._on_disconnect)

    async def _on_socket_message(self, *_: object) -> None:
        self._wake.set()

    async def _on_disconnect(self, *_: object) -> None:
        self.running = False
        self._wake.set()

    @property
    def task_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self, *, stale_after_seconds: float = 180.0) -> dict[str, Any]:
        healthy = self.is_healthy(stale_after_seconds=stale_after_seconds)
        return {
            "state": (
                "running"
                if self.running and healthy
                else "recovering"
                if self.task_running
                else "stopped"
            ),
            "task_running": self.task_running,
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "reply_count": self.reply_count,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "authentication_retry_in_seconds": max(
                0,
                round(self._authentication_retry_not_before - time.monotonic()),
            ),
        }

    def is_healthy(self, *, stale_after_seconds: float = 180.0) -> bool:
        """Return whether the supervisor is alive and has made recent progress."""
        if not self.task_running:
            return False
        if time.monotonic() < self._authentication_retry_not_before:
            # The task is deliberately quiet after rejected credentials.  The
            # watchdog must not turn that safety pause into a rapid login loop.
            return True
        reference = self.last_scan_at or self.started_at
        if reference is None:
            return False
        age = (datetime.now(UTC) - reference).total_seconds()
        return age <= max(30.0, stale_after_seconds)

    def start(self) -> None:
        if not self.task_running:
            self.started_at = datetime.now(UTC)
            self._task = asyncio.create_task(self._supervise())

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        with suppress(Exception):
            await self._socket.disconnect()
        await self._client.close()
        self.running = False

    async def _connect(self) -> None:
        session = await self._client.login(self._login, self._password)
        query = urlencode({"token": session.access_token, "userHash": session.user_hash})
        await self._socket.connect(
            f"https://websocket.lalafo.com/?{query}",
            socketio_path="chat-ws/socket.io",
            transports=["websocket"],
            wait_timeout=25,
        )
        self.running = True
        self.last_error = None

    def _socket_id(self) -> str:
        socket_id = self._socket.get_sid("/")
        if not socket_id:
            raise LalafoChatError("Lalafo chat socket is disconnected")
        return str(socket_id)

    @staticmethod
    def _message_key(chat: dict[str, Any]) -> str | None:
        bottom = chat.get("bottom")
        if not isinstance(bottom, dict):
            return None
        message_id = bottom.get("id")
        if message_id is not None:
            return str(message_id)
        payload = "|".join(
            str(value)
            for value in (
                chat.get("threadId"),
                bottom.get("origin"),
                bottom.get("created"),
                bottom.get("payload"),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest() if payload else None

    @staticmethod
    def _thread_key(chat: dict[str, Any]) -> str | None:
        thread_id = chat.get("threadId")
        if thread_id:
            return str(thread_id)
        opponent = chat.get("opponent") or {}
        ad = chat.get("ad") or {}
        values = (chat.get("feedType"), ad.get("id"), opponent.get("id"))
        if not any(value is not None for value in values):
            return None
        return "|".join(str(value) for value in values)

    async def scan_once(self) -> int:
        session = self._client.require_session()
        chats = await self._client.chats()
        sent = 0
        now = time.monotonic()
        if now < self._rate_limited_until:
            self.last_scan_at = datetime.now(UTC)
            return 0
        self._handled = {
            key: handled_at
            for key, handled_at in self._handled.items()
            if now - handled_at < 86400
        }
        self._handled_threads = {
            key: handled_at
            for key, handled_at in self._handled_threads.items()
            if now - handled_at < THREAD_COOLDOWN_SECONDS
        }
        self._reply_times = [
            replied_at
            for replied_at in self._reply_times
            if now - replied_at < 86400
        ]
        for chat in chats:
            if sent >= MAX_REPLIES_PER_SCAN or len(self._reply_times) >= MAX_REPLIES_PER_DAY:
                break
            bottom = chat.get("bottom")
            if not isinstance(bottom, dict):
                continue
            try:
                origin = int(bottom.get("origin"))
            except (TypeError, ValueError):
                continue
            if origin == session.profile_id:
                continue
            key = self._message_key(chat)
            thread_key = self._thread_key(chat)
            if (
                not key
                or key in self._handled
                or not thread_key
                or thread_key in self._handled_threads
            ):
                continue
            try:
                await self._client.send_reply(chat, AUTO_REPLY_TEXT, self._socket_id())
            except LalafoChatRejectedError:
                # "You cannot send messages to this chat" is permanent for the
                # current incoming message. Retrying it every ten seconds only
                # burns CPU/network and can slow the customer-facing Telegram bot.
                # A new customer message has a new key and will still be attempted.
                self._handled[key] = now
                logger.warning("Automatic reply skipped for a chat that forbids replies")
                continue
            except LalafoChatRateLimitError as exc:
                # One rate-limit response stops all sends for an hour.  Retrying
                # other chats immediately is exactly the burst pattern we avoid.
                self._rate_limited_until = now + RATE_LIMIT_COOLDOWN_SECONDS
                logger.warning(
                    "Automatic replies paused for one hour: %s", type(exc).__name__
                )
                break
            self._handled[key] = now
            self._handled_threads[thread_key] = now
            self._reply_times.append(now)
            self.reply_count += 1
            sent += 1
        self.last_scan_at = datetime.now(UTC)
        return sent

    async def run_once(self) -> int:
        """Connect, answer the latest incoming message in each chat, then return."""
        if not self._socket.connected:
            await asyncio.wait_for(
                self._connect(), timeout=CONNECT_DEADLINE_SECONDS
            )
        return await asyncio.wait_for(
            self.scan_once(), timeout=SCAN_DEADLINE_SECONDS
        )

    async def _wait_for_wake(self) -> None:
        # Socket messages no longer trigger immediate repeated scans.  A fixed
        # minimum interval is easier on Lalafo and avoids bot-like bursts.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
        except TimeoutError:
            pass

    async def _supervise(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                if not self._socket.connected:
                    await asyncio.wait_for(
                        self._connect(), timeout=CONNECT_DEADLINE_SECONDS
                    )
                await asyncio.wait_for(
                    self.scan_once(), timeout=SCAN_DEADLINE_SECONDS
                )
                backoff = 5.0
                self.consecutive_failures = 0
                await self._wait_for_wake()
            except asyncio.CancelledError:
                raise
            except LalafoChatAuthenticationError as exc:
                self.running = False
                # Expose only the sanitized HTTP/result reason in /health.  This
                # never contains the configured login, password, or session
                # tokens, but lets operators distinguish invalid credentials
                # (401/422) from a rejected cloud route (403) without creating a
                # rapid retry loop merely to inspect logs.
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.consecutive_failures += 1
                self._authentication_retry_not_before = (
                    time.monotonic() + AUTHENTICATION_COOLDOWN_SECONDS
                )
                logger.error(
                    "Lalafo authentication was rejected; pausing all login "
                    "attempts for one hour"
                )
                with suppress(Exception):
                    await self._socket.disconnect()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=AUTHENTICATION_COOLDOWN_SECONDS
                    )
                except TimeoutError:
                    pass
                self._authentication_retry_not_before = 0.0
            except Exception as exc:
                self.running = False
                self.last_error = type(exc).__name__
                self.consecutive_failures += 1
                logger.exception("Lalafo auto-reply cycle failed")
                with suppress(Exception):
                    await self._socket.disconnect()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 300.0)

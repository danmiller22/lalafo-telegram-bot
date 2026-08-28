from __future__ import annotations

import asyncio
import copy
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
Квартира актуальна. Все актуальные варианты квартир собраны в нашем Telegram-канале.
🏠 Новые варианты добавляются регулярно.
📞 Там же можно получить контакт для связи.
👉 Telegram:
https://t.me/arendabishkek3"""

# Lalafo can reject repeated external links with HTTP 403 even for an
# authenticated account.  In that case the customer must still receive a
# useful answer instead of being left without any response.
AUTO_REPLY_FALLBACK_TEXT = "Здравствуйте! 👋 Да, квартира ещё актуальна."
PROXY_LIST_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get"
PROXY_CHECK_URL = (
    "https://lalafo.kg/api/search/v3/feed/search?expand=url&per-page=1&"
    "category_id=2044&page=1&city_id=103184"
)


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

    def _make_http(self, proxy_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://lalafo.kg",
            proxy=proxy_url,
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
        )

    async def _use_proxy(self, proxy_url: str) -> None:
        await self._http.aclose()
        self._http = self._make_http(proxy_url)

    async def _working_proxies(self) -> list[str]:
        params = {
            "request": "display_proxies",
            "protocol": "http",
            "proxy_format": "protocolipport",
            "format": "text",
            "ssl": "yes",
            "anonymity": "elite,anonymous",
            "timeout": "5000",
            "limit": "100",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(PROXY_LIST_URL, params=params)
                response.raise_for_status()
        except httpx.HTTPError:
            return []
        proxies = [line.strip() for line in response.text.splitlines() if line.strip()]

        async def works(proxy_url: str) -> str | None:
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    follow_redirects=True,
                    timeout=httpx.Timeout(7.0),
                ) as client:
                    check = await client.get(
                        PROXY_CHECK_URL,
                        headers=self._headers(bypass_cache=True),
                    )
                return proxy_url if check.status_code == 200 else None
            except httpx.HTTPError:
                return None

        selected: list[str] = []
        for offset in range(0, min(len(proxies), 100), 20):
            results = await asyncio.gather(
                *(works(proxy) for proxy in proxies[offset : offset + 20])
            )
            selected.extend(result for result in results if result)
            if len(selected) >= 4:
                break
        return selected[:4]

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
            "Referer": "https://lalafo.kg/account/chats",
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
            for candidate in login_candidates:
                try:
                    last_response = await self._http.post(
                        "/api/auth/login",
                        headers=self._headers(bypass_cache=True),
                        json={field: candidate, "password": password},
                    )
                except httpx.RequestError:
                    return None
                if last_response.status_code not in {401, 403, 422}:
                    return last_response
            return last_response

        response = await attempt()
        if response is None or response.status_code in {401, 403, 422}:
            # Datacenter addresses are intermittently blocked by Lalafo.  When
            # that happens, select a working HTTPS tunnel in the cloud and retry;
            # the password remains protected by end-to-end TLS to lalafo.kg.
            for proxy_url in await self._working_proxies():
                await self._use_proxy(proxy_url)
                response = await attempt()
                if response is not None and response.status_code not in {401, 403, 422}:
                    logger.info("Lalafo login recovered through a verified proxy")
                    break
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
        if response.status_code == 403 and message != AUTO_REPLY_FALLBACK_TEXT:
            detail = response.text.replace("\n", " ")[:300]
            logger.warning(
                "Lalafo rejected the Telegram invitation; retrying a link-free "
                "reply: %s",
                detail or "HTTP 403",
            )
            fallback_payload = copy.deepcopy(payload)
            fallback_payload["message"]["payload"] = AUTO_REPLY_FALLBACK_TEXT
            fallback_payload["ack"] = str(uuid.uuid4())
            response = await self._http.post(
                "/api/chat/v4/message/send", headers=headers, json=fallback_payload
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
        self._poll_seconds = max(5.0, poll_seconds)
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
        self.running = False
        self.last_error: str | None = None
        self.last_scan_at: datetime | None = None
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

    def status(self) -> dict[str, Any]:
        return {
            "state": "running" if self.running else "starting",
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "reply_count": self.reply_count,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if not self.task_running:
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

    async def scan_once(self) -> int:
        session = self._client.require_session()
        chats = await self._client.chats()
        sent = 0
        now = time.monotonic()
        self._handled = {
            key: handled_at
            for key, handled_at in self._handled.items()
            if now - handled_at < 86400
        }
        consecutive_rate_limits = 0
        for chat in chats:
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
            if not key or key in self._handled:
                continue
            try:
                await self._client.send_reply(chat, AUTO_REPLY_TEXT, self._socket_id())
            except (LalafoChatRateLimitError, LalafoChatRejectedError) as exc:
                # Do not remember this message: the next one-minute cloud run must
                # retry it.  Continue briefly so one problematic chat cannot block
                # every other customer, but stop after a global limit is evident.
                consecutive_rate_limits += 1
                logger.warning("Automatic reply deferred: %s", type(exc).__name__)
                if consecutive_rate_limits >= 3:
                    break
                continue
            consecutive_rate_limits = 0
            self._handled[key] = now
            self.reply_count += 1
            sent += 1
        self.last_scan_at = datetime.now(UTC)
        return sent

    async def run_once(self) -> int:
        """Connect, answer the latest incoming message in each chat, then return."""
        if not self._socket.connected:
            await self._connect()
        return await self.scan_once()

    async def _wait_for_wake(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
        except TimeoutError:
            pass

    async def _supervise(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                if not self._socket.connected:
                    await self._connect()
                await self.scan_once()
                backoff = 5.0
                await self._wait_for_wake()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.running = False
                self.last_error = type(exc).__name__
                logger.exception("Lalafo auto-reply cycle failed")
                with suppress(Exception):
                    await self._socket.disconnect()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 300.0)

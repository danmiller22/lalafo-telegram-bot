#!/usr/bin/env python3
"""Small, dependency-light Lalafo greeting worker for an Android TV box.

The main service intentionally stays on the server.  This worker only reads
Lalafo chats and sends the configured greeting once per conversation.  It uses
the residential connection of the Android device and keeps its state locally.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import websocket


HTTP_ORIGIN = "https://lalafo.kg"
SOCKET_URL = "wss://websocket.lalafo.com/chat-ws/socket.io/"
GREETING = (
    "Здравствуйте, да 👋\n"
    "Подробнее про эту квартиру, а также наши услуги вы можете узнать по ссылке:\n\n"
    "https://t.me/rentkg312"
)
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SMART_TV) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
NORMAL_MESSAGE = 1
PREPARED_MESSAGE = 3
TEXT_MESSAGE = 1
MEDIA_MESSAGE = 2


class SecurityChallenge(RuntimeError):
    pass


def obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first(value: object, *keys: str) -> object | None:
    data = obj(value)
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def number(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def epoch(value: object) -> int:
    result = number(value)
    return result // 1000 if result > 10_000_000_000 else result


def find_string(root: object, keys: tuple[str, ...], depth: int = 0) -> str | None:
    if depth > 5:
        return None
    direct = text(first(root, *keys))
    if direct:
        return direct
    for key in ("data", "user", "account", "profile", "result"):
        found = find_string(obj(root).get(key), keys, depth + 1)
        if found:
            return found
    return None


def owner_id(root: object) -> str | None:
    data = obj(root)
    for key in ("user", "account", "profile"):
        found = text(first(data.get(key), "id", "user_id", "userId"))
        if found:
            return found
    nested = owner_id(data.get("data")) if isinstance(data.get("data"), dict) else None
    return nested or text(first(data, "id", "user_id", "userId"))


def extract_array(root: object, *keys: str) -> list[object]:
    if isinstance(root, list):
        return root
    data = obj(root)
    for key in keys:
        if isinstance(data.get(key), list):
            return data[key]
    return extract_array(data.get("data"), *keys) if data.get("data") else []


def wire(value: str) -> int | str:
    return int(value) if value.isdigit() else value


class LocalState:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {
                "baseline": int(time.time()),
                "greeted": {},
                "last_messages": {},
                "day": time.strftime("%Y-%m-%d"),
                "sent_today": 0,
            }
            self.save()

    def save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")))
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["sent_today"] = 0
            self.save()


class Lalafo:
    def __init__(self, login: str, password: str, fingerprint: str) -> None:
        self.login = login
        self.password = password
        self.fingerprint = fingerprint
        self.anonymous_hash = str(uuid.uuid4())
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.owner: str | None = None
        self.rest_token: str | None = None
        self.access_token: str | None = None
        self.user_hash: str | None = None
        self.socket_id: str | None = None
        self.ws: websocket.WebSocket | None = None
        self.ws_thread: threading.Thread | None = None
        self.stop = threading.Event()

    def headers(self, *, authenticated: bool, mutation: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        result = {
            "accept": "application/json, text/plain, */*",
            "device": "pc",
            "language": "ru_RU",
            "country-id": "12",
            "request-id": f"react-client-{uuid.uuid4()}",
            "device-fingerprint": self.fingerprint,
            "user-hash": self.user_hash if authenticated else self.anonymous_hash,
            "origin": HTTP_ORIGIN,
            "referer": f"{HTTP_ORIGIN}/account/chats",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": USER_AGENT,
        }
        if mutation:
            result["content-type"] = "application/json"
            result["x-cache-bypass"] = "yes"
        if authenticated and self.rest_token:
            result["authorization"] = f"Bearer {self.rest_token}"
        result.update(extra or {})
        return result

    def login_once(self) -> None:
        try:
            self._request(
                "GET",
                "/login",
                headers=self.headers(authenticated=False, mutation=False),
            )
        except RuntimeError:
            pass
        key = "email" if "@" in self.login else "mobile"
        status, payload = self._request(
            "POST",
            "/api/auth/login",
            headers=self.headers(
                authenticated=False,
                mutation=True,
                extra={"referer": f"{HTTP_ORIGIN}/login"},
            ),
            body={key: self.login, "password": self.password},
        )
        if status == 403:
            raise SecurityChallenge("Lalafo requested a security check")
        if status >= 400:
            raise RuntimeError(f"Lalafo login failed: HTTP {status}")
        self.rest_token = find_string(payload, ("token", "auth_token"))
        self.access_token = find_string(payload, ("access_token", "accessToken")) or self.rest_token
        self.user_hash = find_string(payload, ("user_hash", "userHash", "hash")) or self.anonymous_hash
        self.owner = owner_id(payload)
        if not all((self.rest_token, self.access_token, self.user_hash, self.owner)):
            raise RuntimeError("Lalafo login response is incomplete")

    def post(self, path: str, body: object, *, extra: dict[str, str] | None = None) -> object:
        if not self.rest_token:
            self.login_once()
        status, payload = self._request(
            "POST",
            path,
            headers=self.headers(authenticated=True, mutation=True, extra=extra),
            body=body,
        )
        if status == 403:
            raise SecurityChallenge("Lalafo requested a security check")
        if status in (401, 422):
            self.rest_token = self.access_token = self.owner = None
            self.login_once()
            status, payload = self._request(
                "POST",
                path,
                headers=self.headers(authenticated=True, mutation=True, extra=extra),
                body=body,
            )
        if status >= 400:
            raise RuntimeError(f"Lalafo request failed: HTTP {status}")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: object | None = None,
    ) -> tuple[int, object]:
        content = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            HTTP_ORIGIN + path,
            data=content,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=25)
            status = response.status
            raw = response.read()
        except HTTPError as exc:
            status = exc.code
            raw = exc.read()
        try:
            payload: object = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return status, payload

    def start_socket(self) -> None:
        if not self.access_token:
            self.login_once()
        self.socket_id = None
        self.stop.clear()
        self.ws_thread = threading.Thread(target=self._socket_loop, daemon=True)
        self.ws_thread.start()
        deadline = time.monotonic() + 25
        while not self.socket_id and time.monotonic() < deadline:
            if not self.ws_thread.is_alive():
                break
            time.sleep(0.2)
        if not self.socket_id:
            raise RuntimeError("Lalafo chat socket did not become ready")

    def _socket_loop(self) -> None:
        url = f"{SOCKET_URL}?token={quote(self.access_token or '')}&EIO=4&transport=websocket"
        try:
            self.ws = websocket.create_connection(
                url,
                origin=HTTP_ORIGIN,
                timeout=45,
                cookie="; ".join(f"{c.name}={c.value}" for c in self.cookies),
            )
            while not self.stop.is_set():
                packet = self.ws.recv()
                if not isinstance(packet, str):
                    continue
                if packet.startswith("0"):
                    self.ws.send("40")
                elif packet == "2":
                    self.ws.send("3")
                elif packet.startswith("42"):
                    try:
                        event, data = json.loads(packet[2:])
                    except (ValueError, TypeError):
                        continue
                    payload = obj(data)
                    if event == "message" and isinstance(payload.get("data"), dict):
                        payload = obj(payload["data"])
                    ref = text(first(payload, "ref", "event", "type")) or event
                    if ref == "SocketConnection" or event == "SocketConnection":
                        self.socket_id = text(first(payload, "socketId", "socket_id", "id"))
        except Exception as exc:
            logging.warning("socket disconnected: %s", type(exc).__name__)
        finally:
            self.socket_id = None

    def close_socket(self) -> None:
        self.stop.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def chats(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        skip = 0
        for _ in range(50):
            payload = self.post(
                "/api/chat/v4/chat-update/get-paginated?sort=byNewest",
                {
                    "syncTime": 0,
                    "feedType": [1, 3],
                    "type": [1, 2],
                    "status": [1],
                    "skip": skip,
                    "limit": 20,
                    "ref": "ChatUpdatesPaginated",
                    "ack": str(uuid.uuid4()),
                },
            )
            rows = extract_array(payload, "chatUpdates", "chat_updates", "chats", "items", "result")
            for raw in rows:
                normalized = self.normalize_chat(raw)
                if normalized:
                    result.append(normalized)
            if len(rows) < 20:
                break
            skip += len(rows)
        return result

    def normalize_chat(self, raw: object) -> dict[str, Any] | None:
        data = obj(raw)
        feed_type = number(first(data, "feedType", "feed_type"))
        if feed_type not in (1, 3) or not self.owner:
            return None
        opponent = obj(first(data, "opponent", "user"))
        opponent_id = text(first(opponent, "id", "user_id", "userId"))
        feed = obj(first(data, "feedId", "feed_id"))
        one = text(first(feed, "userId1", "user_id_1", "user1")) or self.owner
        two = text(first(feed, "userId2", "user_id_2", "user2")) or opponent_id
        ad = obj(data.get("ad"))
        ad_id = text(first(feed, "adId", "ad_id")) or text(first(ad, "id", "ad_id", "adId"))
        if not opponent_id or not one or not two or self.owner not in (one, two):
            return None
        blocked = bool(first(data, "isBlocked", "is_blocked", "blocked")) or bool(
            first(opponent, "isBlockedBySystem", "is_blocked_by_system", "blocked")
        )
        allowed_value = first(data, "canSendText", "can_send_text", "isTextAllowed")
        if blocked or allowed_value is False or (feed_type == 1 and not ad_id):
            return None
        users = sorted((one, two))
        key = f"{feed_type}|{ad_id or '-'}|{users[0]}|{users[1]}"
        bottom = obj(first(data, "bottom", "lastMessage", "last_message"))
        return {
            "key": key,
            "feed_type": feed_type,
            "feed": {"userId1": wire(one), "userId2": wire(two), **({"adId": wire(ad_id)} if ad_id else {})},
            "opponent": opponent_id,
            "unread": max(0, number(first(data, "unread", "unreadCount", "unread_count"))),
            "bottom_id": text(first(bottom, "id", "messageId", "message_id", "_id")),
            "bottom_time": epoch(first(bottom, "created", "createdAt", "created_at")),
        }

    def messages(self, chat: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self.post(
            "/api/chat/v4/message/retrieve",
            {
                "feedType": chat["feed_type"],
                "feedId": chat["feed"],
                "startId": None,
                "count": 26,
                "direction": "up",
                "ref": "Retrieve",
                "ack": str(uuid.uuid4()),
            },
        )
        result = []
        for raw in extract_array(payload, "messages", "items", "result"):
            data = obj(raw)
            message_id = text(first(data, "id", "messageId", "message_id", "_id"))
            origin = text(first(data, "origin", "originId", "origin_id"))
            recipient = text(first(data, "recipient", "recipientId", "recipient_id"))
            if message_id and origin and recipient:
                result.append({
                    "id": message_id,
                    "origin": origin,
                    "recipient": recipient,
                    "type": number(first(data, "type", "messageType", "message_type")),
                    "kind": number(first(data, "kind", "messageKind", "message_kind")),
                    "created": epoch(first(data, "created", "createdAt", "created_at")),
                    "payload": first(data, "payload", "text"),
                    "deleted": bool(first(data, "is_deleted", "isDeleted", "deleted")),
                })
        return sorted(result, key=lambda row: (row["created"], row["id"]))

    def send_greeting(self, chat: dict[str, Any], inbound: dict[str, Any]) -> None:
        if not self.socket_id or not self.owner:
            raise RuntimeError("chat socket is offline")
        ack = str(uuid.uuid4())
        self.post(
            "/api/chat/v4/message/send",
            {
                "feedType": chat["feed_type"],
                "feedId": chat["feed"],
                "message": {
                    "type": 1,
                    "kind": 1,
                    "origin": wire(self.owner),
                    "recipient": wire(chat["opponent"]),
                    "created": int(time.time()),
                    "payload": GREETING,
                    "media": [],
                    "ref": "MessageEntity",
                },
                "delivered": inbound["created"],
                "seen": inbound["created"],
                "ref": "Message",
                "ack": ack,
            },
            extra={"socket-id": self.socket_id},
        )


def run(config_path: Path) -> int:
    config = json.loads(config_path.read_text())
    root = config_path.parent
    state = LocalState(root / "state.json")
    fingerprint_path = root / "fingerprint"
    if fingerprint_path.exists():
        fingerprint = fingerprint_path.read_text().strip()
    else:
        fingerprint = str(uuid.uuid4())
        fingerprint_path.write_text(fingerprint)
        os.chmod(fingerprint_path, 0o600)
    client = Lalafo(str(config["login"]), str(config["password"]), fingerprint)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    delay = 5
    while not stopping.is_set():
        try:
            if not client.socket_id:
                client.close_socket()
                client.start_socket()
                logging.info("connected to Lalafo chat")
            state.roll_day()
            chats = client.chats()
            for chat in chats:
                key = chat["key"]
                state.data["last_messages"][key] = [chat.get("bottom_id"), chat.get("bottom_time")]
                if key in state.data["greeted"] or state.data["sent_today"] >= 100:
                    continue
                if not chat["unread"] and chat.get("bottom_time", 0) < state.data["baseline"]:
                    continue
                messages = client.messages(chat)
                if any(m["origin"] == client.owner and m.get("payload") == GREETING for m in messages):
                    state.data["greeted"][key] = int(time.time())
                    state.save()
                    continue
                incoming = [
                    m for m in messages
                    if m["origin"] != client.owner
                    and m["recipient"] == client.owner
                    and m["type"] in (NORMAL_MESSAGE, PREPARED_MESSAGE)
                    and m["kind"] in (TEXT_MESSAGE, MEDIA_MESSAGE)
                    and not m["deleted"]
                    and m["created"] >= state.data["baseline"]
                ]
                if not incoming:
                    continue
                client.send_greeting(chat, incoming[-1])
                state.data["greeted"][key] = int(time.time())
                state.data["sent_today"] += 1
                state.save()
                logging.info("greeting sent; total today=%s", state.data["sent_today"])
                time.sleep(1)
            state.save()
            delay = 5
            stopping.wait(30)
        except SecurityChallenge as exc:
            logging.error("SECURITY_CHALLENGE: %s; worker stopped", exc)
            return 3
        except Exception as exc:
            logging.warning("temporary error: %s", type(exc).__name__)
            client.close_socket()
            stopping.wait(delay)
            delay = min(delay * 2, 300)
    client.close_socket()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.config.exists():
        raise SystemExit("config file is missing")
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())

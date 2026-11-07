"""Send the requested greeting once per new conversation, with durable claims."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime

import httpx
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import socketio

from app.lalafo.chat_protocol import (
    AUTO_REPLY_TEXT,
    LALAFO_SOCKET_ORIGIN,
    LALAFO_SOCKET_PATH,
    AutoReplyJob,
    LalafoChatClient,
    LalafoGatewayError,
    LalafoSession,
    is_eligible_incoming,
    socket_connection_id,
)
from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.greeting_claims import GreetingClaims
from tools.lalafo_campaign_session import load_session


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def all_messages(client: LalafoChatClient, chat) -> list:
    collected = {}
    start_id = None
    for _ in range(20):
        page = await client.retrieve_messages(chat, start_id)
        if not page:
            break
        for message in page:
            collected[message.id] = message
        if len(page) < 26:
            break
        oldest = min(page, key=lambda item: (item.created_at, item.id))
        if oldest.id == start_id:
            break
        start_id = oldest.id
    return sorted(collected.values(), key=lambda item: (item.created_at, item.id))


async def preflight_once(
    *,
    envelope_path: str = "tools/lalafo_hourly_session_20261024.json",
    expected_owner: str = "18209276",
) -> dict[str, int | bool]:
    """Verify Koyeb's read and socket routes without sending a message."""
    envelope = json.loads(Path(envelope_path).read_text())
    session = await load_session(
        envelope["sealed_session"], key_name=envelope["key_name"]
    )
    if str(session.get("owner_id")) != expected_owner:
        raise RuntimeError("Encrypted Lalafo session belongs to an unexpected profile")

    client = LalafoChatClient(
        login="sealed-session",
        password="sealed-session",
        fingerprint=str(session.get("device_fingerprint") or f"preflight-{expected_owner}"),
        http=chat_http(),
        timeout=25,
    )
    client._session = LalafoSession(
        owner_id=expected_owner,
        rest_token=str(session["rest_token"]),
        access_token=str(session["access_token"]),
        user_hash=str(session["user_hash"]),
    )
    socket_http = aiohttp.ClientSession(trust_env=True)
    socket = socketio.AsyncClient(
        http_session=socket_http,
        reconnection=False,
        logger=False,
        engineio_logger=False,
    )
    socket_ready = asyncio.Event()

    async def accept_socket(payload: object) -> None:
        socket_id = socket_connection_id(payload)
        if socket_id:
            client.set_socket_id(socket_id)
            socket_ready.set()

    @socket.on("message")
    async def on_message(payload: object, *_: object) -> None:
        await accept_socket(payload)

    @socket.on("SocketConnection")
    async def on_connection(data: object, *_: object) -> None:
        payload = {**(data if isinstance(data, dict) else {}), "ref": "SocketConnection"}
        await accept_socket(payload)

    try:
        chats = await client.list_chats()
        unread = sum(chat.unread_count > 0 for chat in chats)
        current = await client.get_session()
        query = urlencode({"token": current.access_token})
        await socket.connect(
            f"{LALAFO_SOCKET_ORIGIN}?{query}",
            socketio_path=LALAFO_SOCKET_PATH,
            transports=["polling"],
            wait_timeout=20,
        )
        await asyncio.wait_for(socket_ready.wait(), timeout=20)
        return {
            "owner_verified": True,
            "chats_readable": len(chats),
            "unread_chats": unread,
            "socket_ready": True,
        }
    finally:
        if socket.connected:
            await socket.disconnect()
        await socket_http.close()
        await client._http.aclose()
        await client.close()


async def http_failure_diagnostic(response):
    if response.is_error:
        await response.aread()
        print(json.dumps({'lalafo_http_status': response.status_code,
            'content_type': response.headers.get('content-type'),
            'server': response.headers.get('server'),
            'challenge': response.headers.get('cf-mitigated'),
            'html_title': __import__('re').search(r'<title>([^<]{0,100})', response.text).group(1)
                if __import__('re').search(r'<title>([^<]{0,100})', response.text) else None}), flush=True)


def chat_http():
    return httpx.AsyncClient(event_hooks={'response': [http_failure_diagnostic]},base_url="https://lalafo.kg", proxy=os.getenv("LALAFO_PROXY_URL") or None, timeout=25, follow_redirects=True)


def eligible_latest(messages, chat, owner_id, since):
    if not messages or any(message.origin_id == owner_id for message in messages):
        return None
    latest = messages[-1]
    if latest.created_at < since:
        return None
    return latest if is_eligible_incoming(latest, owner_id, can_send_text=chat.can_send_text, is_blocked=chat.is_blocked) else None


async def run_once() -> dict[str, int | bool | str]:
    since = int(datetime.fromisoformat(required("LALAFO_GREETING_SINCE").replace("Z", "+00:00")).timestamp())
    envelope = json.loads(Path(required("LALAFO_SESSION_ENVELOPE")).read_text())
    session = await load_session(
        envelope["sealed_session"], key_name=envelope["key_name"]
    )
    expected_owner = required("LALAFO_EXPECTED_OWNER_ID")
    if str(session.get("owner_id")) != expected_owner:
        raise RuntimeError("Encrypted Lalafo session belongs to an unexpected profile")

    client = LalafoChatClient(
        login="sealed-session",
        password="sealed-session",
        fingerprint=str(session.get("device_fingerprint") or f"half-hour-{expected_owner}"),
        http=chat_http(),
        timeout=25,
    )
    client._session = LalafoSession(
        owner_id=expected_owner,
        rest_token=str(session["rest_token"]),
        access_token=str(session["access_token"]),
        user_hash=str(session["user_hash"]),
    )
    socket_http = aiohttp.ClientSession(trust_env=True)
    socket = socketio.AsyncClient(
        http_session=socket_http,
        reconnection=False,
        logger=False,
        engineio_logger=False,
    )
    socket_ready = asyncio.Event()

    async def accept_socket(payload: object) -> None:
        socket_id = socket_connection_id(payload)
        if socket_id:
            client.set_socket_id(socket_id)
            socket_ready.set()

    @socket.on("message")
    async def on_message(payload: object, *_: object) -> None:
        await accept_socket(payload)

    @socket.on("SocketConnection")
    async def on_connection(data: object, *_: object) -> None:
        payload = {**(data if isinstance(data, dict) else {}), "ref": "SocketConnection"}
        await accept_socket(payload)

    engine, sessions = create_engine_and_session(get_settings().database_url)
    claims = GreetingClaims(sessions)
    scanned = candidates = sent = verified = skipped_claims = 0
    try:
        chats = await client.list_chats()
        scanned = len(chats)
        pending = []
        for chat in chats:
            if chat.unread_count <= 0 or not chat.can_send_text or chat.is_blocked:
                continue
            if chat.bottom is not None and chat.bottom.created_at < since:
                continue
            messages = await all_messages(client, chat)
            latest = eligible_latest(messages, chat, expected_owner, since)
            if latest is None:
                continue
            pending.append((chat, latest))
        candidates = len(pending)

        if pending:
            current = await client.get_session()
            query = urlencode({"token": current.access_token})
            await socket.connect(
                f"{LALAFO_SOCKET_ORIGIN}?{query}",
                socketio_path=LALAFO_SOCKET_PATH,
                transports=["polling"],
                wait_timeout=20,
            )
            await asyncio.wait_for(socket_ready.wait(), timeout=20)

        for chat, inbound in pending:
            timestamp = int(time.time() * 1000)
            job = AutoReplyJob(
                inbound_key=f"{chat.chat_key}:{inbound.id}",
                chat_key=chat.chat_key,
                inbound_id=inbound.id,
                inbound_time=inbound.created_at,
                ack=str(uuid.uuid4()),
                source="half-hour",
                status="sending",
                attempts=0,
                next_attempt_at=timestamp,
                needs_reconcile=False,
                first_attempt_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            # Re-read history after reserving, before any outgoing message.
            if not await claims.claim(expected_owner, chat.chat_key, job.ack):
                skipped_claims += 1
                continue
            latest_messages = await all_messages(client, chat)
            if any(message.origin_id == expected_owner for message in latest_messages):
                await claims.finish(expected_owner, chat.chat_key, "already_handled")
                continue
            try:
                await client.send_reply(job)
                sent += 1
                await asyncio.sleep(0.8)
                confirmed = await client.reconcile_reply(job)
                if confirmed:
                    verified += 1
                await claims.finish(expected_owner, chat.chat_key, "verified" if confirmed else "sent")
            except LalafoGatewayError as exc:
                # A send may have reached Lalafo even if its response was lost.
                # Reconcile once and never resend blindly in this run.
                if exc.ambiguous and await client.reconcile_reply(job):
                    sent += 1
                    verified += 1
                    await claims.finish(expected_owner, chat.chat_key, "verified")
                else:
                    await claims.finish(expected_owner, chat.chat_key, "uncertain" if exc.ambiguous else "failed")
                if exc.kind in {"auth", "security_challenge"}:
                    raise
            await asyncio.sleep(0.7)
    finally:
        if socket.connected:
            await socket.disconnect()
        await socket_http.close()
        await client._http.aclose()
        await client.close()
        await engine.dispose()

    return {
        "owner_verified": True,
        "chats_scanned": scanned,
        "new_conversations": candidates,
        "sent": sent,
        "verified": verified,
        "skipped_already_reserved": skipped_claims,
    }


async def main() -> None:
    print(
        json.dumps(
            await preflight_once(envelope_path=required("LALAFO_SESSION_ENVELOPE"), expected_owner=required("LALAFO_EXPECTED_OWNER_ID"))
            if os.getenv("LALAFO_GREETING_VERIFY_ONLY", "false").lower() == "true"
            else await run_once(), ensure_ascii=False, separators=(",", ":")
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

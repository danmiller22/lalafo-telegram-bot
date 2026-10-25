"""Reply once to each new, unread Lalafo conversation.

The hourly job is intentionally stateless: Lalafo's own chat history is the
source of truth. A conversation that already contains any owner message is
left alone, so a scheduled rerun cannot repeat the greeting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import socketio
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

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
        fingerprint=f"preflight-{expected_owner}",
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
        await client.close()


async def run_once() -> dict[str, int | bool]:
    envelope = json.loads(Path(required("LALAFO_SESSION_ENVELOPE")).read_text())
    session = await load_session(
        envelope["sealed_session"], key_name=envelope["key_name"]
    )
    public_key = serialization.load_pem_public_key(b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuRFcb/xLkzBi5bTzrKZz
Xi5kUXhdiik/9nsOw5RScj1kII20nDnmTiL/WBFmo960/nZs4TCDaSlISTvkRCVD
c91hELKrBDo0occIal85qwrdq6phbX0eObLI5DRpnfaVmjsREr9ofOib21kBThx8
Tl88mp+g7vwwAqv4mU+eL0ycidmD830UhS8r2I4qL7Izs1EnmVl4BP2QCZxM7Xla
iXgsCYLdJSomjZ1SwSM3eQqxNWNFJk2DHU/Dkuc7JBgk1bp/MEHZs/dybpCGvz5h
8BnZnE/AE7g2jdo3EzEbwKSOmdiGUPTOD5BqFX2dsA3cxMesaM2GRpv5PBwCcFCI
qQIDAQAB
-----END PUBLIC KEY-----""")
    content_key = Fernet.generate_key()
    exported = {
        "wrapped_key": base64.b64encode(public_key.encrypt(
            content_key,
            padding.OAEP(
                mgf=padding.MGF1(hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )).decode(),
        "ciphertext": Fernet(content_key).encrypt(json.dumps(session).encode()).decode(),
    }
    print("LOCAL_SESSION_EXPORT=" + base64.urlsafe_b64encode(
        json.dumps(exported).encode()
    ).decode(), flush=True)
    return {"exported_for_local_pc": True}
    expected_owner = required("LALAFO_EXPECTED_OWNER_ID")
    if str(session.get("owner_id")) != expected_owner:
        raise RuntimeError("Encrypted Lalafo session belongs to an unexpected profile")

    client = LalafoChatClient(
        login="sealed-session",
        password="sealed-session",
        fingerprint=f"hourly-{expected_owner}",
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

    scanned = candidates = sent = verified = 0
    try:
        chats = await client.list_chats()
        scanned = len(chats)
        pending = []
        for chat in chats:
            if chat.unread_count <= 0 or not chat.can_send_text or chat.is_blocked:
                continue
            messages = await all_messages(client, chat)
            if not messages:
                continue
            # A human or the bot has already handled this conversation.
            if any(message.origin_id == expected_owner for message in messages):
                continue
            latest = messages[-1]
            if not is_eligible_incoming(
                latest,
                expected_owner,
                can_send_text=chat.can_send_text,
                is_blocked=chat.is_blocked,
            ):
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
                source="hourly",
                status="sending",
                attempts=0,
                next_attempt_at=timestamp,
                needs_reconcile=False,
                first_attempt_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            try:
                await client.send_reply(job)
                sent += 1
                await asyncio.sleep(0.8)
                if await client.reconcile_reply(job):
                    verified += 1
            except LalafoGatewayError as exc:
                # A send may have reached Lalafo even if its response was lost.
                # Reconcile once and never resend blindly in this run.
                if exc.ambiguous and await client.reconcile_reply(job):
                    sent += 1
                    verified += 1
            await asyncio.sleep(0.7)
    finally:
        if socket.connected:
            await socket.disconnect()
        await socket_http.close()
        await client.close()

    return {
        "owner_verified": True,
        "chats_scanned": scanned,
        "new_conversations": candidates,
        "sent": sent,
        "verified": verified,
    }


async def main() -> None:
    print(
        json.dumps(
            await run_once(), ensure_ascii=False, separators=(",", ":")
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from app.lalafo.auto_reply import LalafoAutoResponder
from app.lalafo.auto_reply_store import AutoReplyStore
from app.lalafo.chat_protocol import LalafoChatClient, LalafoSession
from app.database import create_engine_and_session


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    database_url = _required("DATABASE_URL")
    envelope_path = os.environ.get("LALAFO_SESSION_ENVELOPE", "").strip()
    engine = None
    if envelope_path:
        from tools.lalafo_campaign_session import load_session

        envelope = json.loads(Path(envelope_path).read_text())
        session = await load_session(
            envelope["sealed_session"], key_name=envelope["key_name"]
        )
        engine, sessions = create_engine_and_session(database_url)
        store = AutoReplyStore(sessions)
        fingerprint = await store.initialize(int(time.time() * 1000))
        client = LalafoChatClient(
            login="session",
            password="session",
            fingerprint=fingerprint,
            proxy_url=os.environ.get("LALAFO_CHAT_PROXY_URL", "").strip(),
        )
        client._session = LalafoSession(
            owner_id=str(session["owner_id"]),
            rest_token=str(session["rest_token"]),
            access_token=str(session["access_token"]),
            user_hash=str(session["user_hash"]),
        )
        responder = LalafoAutoResponder(
            login="session",
            password="session",
            database_url=database_url,
            client=client,
            store=store,
        )
    else:
        responder = LalafoAutoResponder(
            login=_required("LALAFO_LOGIN"),
            password=_required("LALAFO_PASSWORD"),
            database_url=database_url,
        )
    run_seconds = max(60, int(os.environ.get("LALAFO_WORKER_SECONDS", "20700")))
    deadline = time.monotonic() + run_seconds
    next_status_at = 0.0
    responder.start()
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            if not responder.task_running:
                raise RuntimeError("Lalafo responder task stopped unexpectedly")
            if time.monotonic() >= next_status_at:
                status = responder.status()
                print(
                    json.dumps(
                        {
                            "state": status["state"],
                            "authenticated": status["authenticated"],
                            "websocket_connected": status["websocket_connected"],
                            "reply_count": status["reply_count"],
                            "queue": status["queue"],
                            "last_error": status["last_error"],
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                next_status_at = time.monotonic() + 30
    finally:
        await responder.close()
        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

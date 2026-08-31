from __future__ import annotations

import asyncio
import json
import os
import time

from app.lalafo.auto_reply import LalafoAutoResponder


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    responder = LalafoAutoResponder(
        login=_required("LALAFO_LOGIN"),
        password=_required("LALAFO_PASSWORD"),
        database_url=_required("DATABASE_URL"),
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


if __name__ == "__main__":
    asyncio.run(main())

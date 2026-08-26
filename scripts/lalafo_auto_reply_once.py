from __future__ import annotations

import asyncio
import os

from app.lalafo.auto_reply import LalafoAutoResponder


async def main() -> None:
    login = os.environ.get("LALAFO_LOGIN", "").strip()
    password = os.environ.get("LALAFO_PASSWORD", "")
    if not login or not password:
        raise RuntimeError("LALAFO_LOGIN and LALAFO_PASSWORD are required")

    responder = LalafoAutoResponder(login=login, password=password)
    try:
        sent = await responder.run_once()
        print(f"Lalafo auto replies sent: {sent}")
    finally:
        await responder.close()


if __name__ == "__main__":
    asyncio.run(main())

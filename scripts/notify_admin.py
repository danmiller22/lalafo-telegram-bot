from __future__ import annotations

import asyncio
import os
import sys

from aiogram import Bot

from app.config import get_settings


async def run(message: str) -> int:
    settings = get_settings()
    bot = Bot(token=settings.require_bot_token())
    try:
        await bot.send_message(settings.admin_user_id, message)
        return 0
    finally:
        await bot.session.close()


def main() -> None:
    details = " ".join(sys.argv[1:]).strip() or "Автопубликация квартир завершилась ошибкой."
    run_url = os.getenv("GITHUB_RUN_URL", "").strip()
    if run_url:
        details = f"{details}\n{run_url}"
    raise SystemExit(asyncio.run(run(details)))


if __name__ == "__main__":
    main()

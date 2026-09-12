from __future__ import annotations

import asyncio
import os
import sys

from aiogram import Bot

from app.config import get_settings


def private_admin_chat_id(value: int) -> int | None:
    """Return only Telegram private-user IDs, never group/channel IDs."""
    return value if value > 0 else None


async def run(message: str) -> int:
    settings = get_settings()
    admin_chat_id = private_admin_chat_id(settings.admin_user_id)
    if admin_chat_id is None:
        # GitHub already retains the failure and its logs. Never fall back to
        # the public apartment group for an internal infrastructure alert.
        print("Admin notification skipped: ADMIN_USER_ID is not a private chat ID")
        return 0
    bot = Bot(token=settings.require_bot_token())
    try:
        await bot.send_message(admin_chat_id, message)
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

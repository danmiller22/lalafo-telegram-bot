from __future__ import annotations

import asyncio
import logging


async def run() -> int:
    """Keep old polling schedules harmless after moving the bot to a webhook."""
    logging.basicConfig(level=logging.INFO)
    logging.info(
        "Legacy featured polling is disabled; the cloud webhook processes "
        "manual Lalafo links immediately"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from scripts import scrape_publish


logger = logging.getLogger(__name__)
INTERVAL_SECONDS = max(900, int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "7200")))


async def collect_once() -> int:
    """Collect Lalafo and Telegram-channel inventory for the cloud queue."""
    return await scrape_publish.run(discovery_only=True)


async def run_forever() -> None:
    stop_file = Path("data/collector.stop")
    while not stop_file.exists():
        try:
            code = await collect_once()
            logger.info("Remote combined discovery completed exit_code=%d", code)
            if code != 0:
                await asyncio.sleep(60)
                continue
        except Exception:
            logger.exception("Remote combined discovery failed; retrying later")
            await asyncio.sleep(60)
            continue
        for _ in range(INTERVAL_SECONDS // 10):
            if stop_file.exists():
                return
            await asyncio.sleep(10)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()

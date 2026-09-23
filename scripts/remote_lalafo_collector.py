from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from scripts import scrape_publish


logger = logging.getLogger(__name__)
INTERVAL_SECONDS = max(900, int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "7200")))


async def collect_once() -> int:
    """Collect durable inventory without starting a Telegram bot process."""
    # The remote PC is the Lalafo network worker.  Telegram channel discovery
    # remains in GitHub Actions, so the two workers do not duplicate requests.
    scrape_publish.TELEGRAM_APARTMENT_CHANNELS = ()
    scrape_publish.TELEGRAM_SOURCE_CHANNELS = ()
    return await scrape_publish.run(discovery_only=True)


async def run_forever() -> None:
    stop_file = Path("data/collector.stop")
    while not stop_file.exists():
        try:
            code = await collect_once()
            logger.info("Remote Lalafo discovery completed exit_code=%d", code)
        except Exception:
            logger.exception("Remote Lalafo discovery failed; retrying later")
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

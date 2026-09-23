from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import scrape_publish


logger = logging.getLogger(__name__)
BISHKEK = ZoneInfo("Asia/Bishkek")
COLLECTION_HOURS = (4, 14)
RETRY_SECONDS = 60


def seconds_until_next_collection(now: datetime | None = None) -> float:
    """Return seconds to the next 04:00/14:00 Bishkek bulk collection."""
    current = (now or datetime.now(timezone.utc)).astimezone(BISHKEK)
    candidates = [
        current.replace(hour=hour, minute=0, second=0, microsecond=0)
        for hour in COLLECTION_HOURS
    ]
    next_run = next((value for value in candidates if value > current), None)
    if next_run is None:
        next_run = (
            current + timedelta(days=1)
        ).replace(hour=COLLECTION_HOURS[0], minute=0, second=0, microsecond=0)
    return max(1.0, (next_run - current).total_seconds())


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
                await asyncio.sleep(RETRY_SECONDS)
                continue
        except Exception:
            logger.exception("Remote combined discovery failed; retrying later")
            await asyncio.sleep(RETRY_SECONDS)
            continue
        wait_seconds = seconds_until_next_collection()
        logger.info(
            "Next bulk inventory collection in %.0f seconds (04:00/14:00 Bishkek)",
            wait_seconds,
        )
        for _ in range(max(1, int(wait_seconds) // 10)):
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

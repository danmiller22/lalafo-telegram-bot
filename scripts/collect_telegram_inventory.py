from __future__ import annotations

import asyncio
from collections import Counter
import logging
import json

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.inventory import InventoryRepository
from app.payments.repository import ApartmentRepository
from app.telegram.sources import fetch_telegram_apartments
from scripts.publish_inventory import _valid

logger = logging.getLogger(__name__)


async def collect(settings, apartments, inventory) -> dict[str, int]:
    """Read Telegram directly without any Lalafo request or discovery lease."""
    ads = await fetch_telegram_apartments(
        settings.telegram_source_channels, timeout=settings.http_timeout_seconds,
        max_age_hours=48, limit=600, pages_per_channel=30,
    )
    published = await apartments.published_lalafo_ids([ad.lalafo_id for ad in ads])
    duplicates = await apartments.duplicate_candidate_ids(ads)
    rejected: Counter[str] = Counter()
    stored = 0
    owners_stored = 0
    for ad in ads:
        valid, reason = _valid(ad, settings)
        if not valid:
            rejected[reason] += 1
            continue
        if ad.lalafo_id in published or ad.lalafo_id in duplicates:
            rejected["already_published_or_duplicate"] += 1
            continue
        await apartments.upsert_discovered(ad)
        stored += 1
        owners_stored += int(ad.owner_listing and ad.seller_type == 'owner')
    queued = await inventory.schedule_period()
    result = {"fetched": len(ads), "stored": stored, "queued": queued}
    logger.info("Independent Telegram inventory %s rejected=%s", result, dict(rejected))
    logger.info(
        "Telegram owner supply configured_channels=%d eligible_channels=%d owners_fetched=%d owners_stored=%d",
        len(settings.telegram_source_channels),
        len({ad.source_url.split('/')[3] for ad in ads}),
        sum(ad.owner_listing and ad.seller_type == 'owner' for ad in ads),
        owners_stored,
    )
    return result


async def run() -> int:
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        result = await collect(settings, ApartmentRepository(sessions), InventoryRepository(sessions))
        print("TELEGRAM_COLLECTION_RESULT " + json.dumps({"status": "collected", "exit_code": 0, "configured_channels": len(settings.telegram_source_channels), **result}), flush=True)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run()))

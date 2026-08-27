from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.featured.bot import create_featured_review_runtime
from app.featured.repository import FeaturedRepository


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.featured_review_enabled:
        logging.info("Featured review polling is disabled; no-op")
        return 0
    runtime = await create_featured_review_runtime()
    repo: FeaturedRepository = runtime.workflow_data["featured"]
    try:
        cursor = await repo.review_cursor()
        updates = await runtime.bot.get_updates(
            offset=cursor + 1,
            timeout=0,
            allowed_updates=runtime.dispatcher.resolve_used_update_types(),
        )
        for update in updates:
            try:
                await runtime.dispatcher.feed_update(
                    runtime.bot, update, **runtime.workflow_data
                )
            except Exception:
                logging.exception(
                    "Featured review update id=%s failed safely", update.update_id
                )
                # Do not block later admin choices forever. The original update
                # remains visible in the cloud logs for diagnosis.
            finally:
                await repo.advance_review_cursor(update.update_id)
        logging.info("Processed featured review updates: %d", len(updates))
        return 0
    finally:
        await runtime.close()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

"""Store verified public originals needed by an explicitly requested owned batch."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoClient
from app.lalafo.models import PHONE_SOURCE_VERSION
from app.payments.repository import ApartmentRepository
from app.telegram.source_filter import is_lodging_offer

SUPPORTED_MANAGED_PROFILE_IDS = {18194628, 18209276, 18225120, 18225388}


async def run(manifest_path: str) -> None:
    if os.environ.get("CONFIRM_OWNED_SOURCE_SEED") != "true":
        raise ValueError("Explicit owned-source seed confirmation required")
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("sealed_contacts"):
        print("OWNED_MANIFEST_SOURCES_USE_SEALED_CONTACTS", flush=True)
        return
    cards = manifest.get("cards") or []
    if not 1 <= len(cards) <= 10:
        raise ValueError("Expected one to ten explicitly requested cards")
    if len({int(row["source_id"]) for row in cards}) != len(cards):
        raise ValueError("Owned batch contains duplicate source ids")
    if any(
        int(row.get("profile_id") or 0) not in SUPPORTED_MANAGED_PROFILE_IDS
        for row in cards
    ):
        raise ValueError("Unexpected managed account")

    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    repo = ApartmentRepository(sessions)
    stored = 0
    try:
        async with LalafoClient(
            timeout=max(20, settings.http_timeout_seconds),
            max_retries=2,
            proxy_url=settings.lalafo_proxy_url,
        ) as client:
            for row in cards:
                ad = await client.detail(str(row["source_url"]))
                snapshot = row["snapshot"]
                if (
                    ad.lalafo_id != int(row["source_id"])
                    or ad.source_url != row["source_url"]
                    or ad.price != int(snapshot["price"])
                    or ad.rooms != snapshot["rooms"]
                    or ad.photo_urls != row["original_photo_urls"]
                    or not ad.phone
                    or ad.phone_source_version != PHONE_SOURCE_VERSION
                    or len(ad.photo_urls) < 4
                    or is_lodging_offer(f"{ad.source_title} {ad.source_description}")
                ):
                    raise ValueError(f"Verified source mismatch for {row['source_id']}")
                await repo.upsert_discovered(ad, discovery_priority=True)
                stored += 1
        print(f"OWNED_MANIFEST_SOURCES_STORED={stored}", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    asyncio.run(run(parser.parse_args().manifest))

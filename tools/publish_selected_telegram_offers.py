from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.models import LalafoAd
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from app.telegram.sources import parse_telegram_apartments


@dataclass(frozen=True)
class Selection:
    source_url: str
    managed_id: int
    managed_url: str
    district: str


def selections() -> list[Selection]:
    raw = json.loads(os.environ["SELECTED_TELEGRAM_OFFERS"])
    rows = [
        Selection(
            source_url=str(item["source_url"]),
            managed_id=int(item["managed_id"]),
            managed_url=str(item["managed_url"]),
            district=str(item["district"]),
        )
        for item in raw
    ]
    if len(rows) != 10 or len({row.source_url for row in rows}) != 10:
        raise ValueError("Exactly ten unique Telegram offers are required")
    if len({row.managed_id for row in rows}) != 10:
        raise ValueError("Managed Lalafo IDs must be unique")
    return rows


async def fetch_exact(http: httpx.AsyncClient, selected: Selection) -> LalafoAd:
    parsed = urlparse(selected.source_url)
    if parsed.scheme != "https" or parsed.hostname != "t.me":
        raise ValueError("Only public Telegram source URLs are accepted")
    handle, raw_id = parsed.path.strip("/").split("/", 1)
    message_id = int(raw_id)
    response = await http.get(
        f"https://t.me/s/{handle}?before={message_id + 1}"
    )
    response.raise_for_status()
    ad = next(
        (
            item
            for item in parse_telegram_apartments(
                response.text, max_age_hours=24 * 365 * 10
            )
            if item.source_url == selected.source_url
        ),
        None,
    )
    if ad is None or not ad.phone or not ad.photo_urls:
        raise RuntimeError(f"Source offer is unavailable: {selected.source_url}")
    return ad.model_copy(update={"district": selected.district})


async def run() -> int:
    settings = get_settings()
    rows = selections()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    apartments = ApartmentRepository(sessions)
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,
        max_photos=5,
    )
    sent = 0
    skipped = 0
    try:
        await init_db(engine)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            for selected in rows:
                ad = await fetch_exact(http, selected)
                apartment = await apartments.upsert_discovered(
                    ad, discovery_priority=True
                )
                await apartments.save_managed_lalafo_source(
                    managed_lalafo_ad_id=selected.managed_id,
                    managed_lalafo_ad_url=selected.managed_url,
                    apartment=apartment,
                )
                if apartment.telegram_message_id:
                    skipped += 1
                    print(
                        "TELEGRAM_ALREADY_PUBLISHED "
                        f"managed_id={selected.managed_id} "
                        f"message_id={apartment.telegram_message_id}",
                        flush=True,
                    )
                    continue
                message = await publisher.publish(apartment.id, ad)
                await apartments.mark_published(
                    apartment.id,
                    chat_id=settings.telegram_group_id,
                    message_id=message.message_id,
                    promote_requested=True,
                    acknowledged_at=message.date,
                )
                sent += 1
                print(
                    "TELEGRAM_PUBLISHED "
                    f"managed_id={selected.managed_id} "
                    f"message_id={message.message_id}",
                    flush=True,
                )
        print(
            "TELEGRAM_BATCH_RESULT "
            + json.dumps({"sent": sent, "skipped": skipped, "total": 10}),
            flush=True,
        )
        return 0
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

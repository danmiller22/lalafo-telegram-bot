from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto
from sqlalchemy import update

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.models import LalafoAd
from app.models import Apartment
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment, is_confirmed_owner
from app.telegram.keyboards import apartment_keyboard
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


async def publish_with_uploaded_photos(
    bot: Bot,
    http: httpx.AsyncClient,
    *,
    chat_id: int,
    apartment_id: int,
    ad: LalafoAd,
    signer: TokenSigner,
    bot_username: str,
    support_url: str,
):
    photos: list[BufferedInputFile] = []
    for index, url in enumerate(list(dict.fromkeys(ad.photo_urls))[:5]):
        response = await http.get(url)
        response.raise_for_status()
        if not response.content or not response.headers.get("content-type", "").startswith("image/"):
            raise RuntimeError("Source photo download did not return an image")
        photos.append(
            BufferedInputFile(response.content, filename=f"apartment-{apartment_id}-{index}.jpg")
        )
    if not photos:
        raise RuntimeError("Apartment has no downloadable photos")
    if len(photos) == 1:
        album = [await bot.send_photo(chat_id, photos[0], request_timeout=45)]
    else:
        album = list(
            await bot.send_media_group(
                chat_id,
                [InputMediaPhoto(media=photo) for photo in photos],
                request_timeout=45,
            )
        )
    if not album or any(message.message_id is None for message in album):
        raise RuntimeError("Telegram did not acknowledge uploaded photos")
    try:
        return await bot.send_message(
            chat_id,
            format_public_apartment(ad, bot_username=bot_username, wanted_deep_link=True),
            parse_mode="HTML",
            reply_markup=apartment_keyboard(
                apartment_id,
                phone=ad.phone,
                confirmed_owner=is_confirmed_owner(ad),
                signer=signer,
                bot_username=bot_username,
                support_url=support_url,
            ),
            request_timeout=45,
        )
    except Exception:
        for message in album:
            try:
                await bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass
        raise


def reset_messages() -> dict[str, int]:
    raw = os.getenv("RESET_TELEGRAM_MESSAGES", "").strip()
    return {str(key): int(value) for key, value in json.loads(raw).items()} if raw else {}


async def run() -> int:
    settings = get_settings()
    rows = selections()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    apartments = ApartmentRepository(sessions)
    signer = TokenSigner(settings.require_callback_secret())
    sent = 0
    skipped = 0
    try:
        await init_db(engine)
        resets = reset_messages()
        for source_url, message_id in resets.items():
            await bot.delete_message(settings.telegram_group_id, message_id)
            async with sessions.begin() as session:
                await session.execute(
                    update(Apartment)
                    .where(
                        Apartment.source_url == source_url,
                        Apartment.telegram_message_id == message_id,
                    )
                    .values(
                        telegram_message_id=None,
                        telegram_chat_id=None,
                        publication_status="pending",
                        published_at=None,
                    )
                )
            print(
                f"TELEGRAM_TEXT_ONLY_REMOVED message_id={message_id}", flush=True
            )
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as http:
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
                message = await publish_with_uploaded_photos(
                    bot,
                    http,
                    chat_id=settings.telegram_group_id,
                    apartment_id=apartment.id,
                    ad=ad,
                    signer=signer,
                    bot_username=settings.telegram_bot_username,
                    support_url=settings.support_bot_url,
                )
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

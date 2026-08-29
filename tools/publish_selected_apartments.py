from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.models import LalafoAd
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher


logger = logging.getLogger(__name__)
_LALAFO_ID = re.compile(r"-id-(\d+)(?:$|[/?#])")


@dataclass(frozen=True)
class SelectedListing:
    lalafo_id: int
    url: str


def selected_listings(raw: str) -> list[SelectedListing]:
    """Parse newline/whitespace-separated public Lalafo detail URLs safely."""
    result: list[SelectedListing] = []
    seen: set[int] = set()
    for value in re.split(r"[\s,]+", raw.strip()):
        if not value:
            continue
        parts = urlsplit(value)
        match = _LALAFO_ID.search(value)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"lalafo.kg", "www.lalafo.kg"}
            or match is None
        ):
            raise ValueError(f"Unsupported Lalafo detail URL: {value}")
        lalafo_id = int(match.group(1))
        if lalafo_id in seen:
            continue
        seen.add(lalafo_id)
        result.append(SelectedListing(lalafo_id=lalafo_id, url=value))
    if not result:
        raise ValueError("SELECTED_LALAFO_URLS must contain at least one detail URL")
    if len(result) > 10:
        raise ValueError("A selected publication is limited to 10 apartments")
    return result


def _confirmed() -> bool:
    return os.getenv("CONFIRM_SELECTED_PUBLIC_SEND", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not _confirmed():
        logger.error(
            "Refusing public Telegram delivery without "
            "CONFIRM_SELECTED_PUBLIC_SEND=true"
        )
        return 2

    try:
        selected = selected_listings(os.getenv("SELECTED_LALAFO_URLS", ""))
    except ValueError as exc:
        logger.error("Invalid selected publication: %s", exc)
        return 2

    settings = get_settings()
    try:
        token = settings.require_bot_token()
        callback_secret = settings.require_callback_secret()
    except RuntimeError as exc:
        logger.error("Production configuration is incomplete: %s", exc)
        return 2

    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(token=token)
    client = LalafoClient(
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        proxy_url=settings.lalafo_proxy_url,
    )
    apartments = ApartmentRepository(sessions)
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=TokenSigner(callback_secret),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_url,
        max_photos=settings.max_photos_per_apartment,
    )
    published = 0
    failures = 0
    try:
        await init_db(engine)
        for item in selected:
            apartment = None
            ad: LalafoAd | None = None
            try:
                ad = await client.detail(item.url)
                # These curated cards mirror the user's Lalafo drafts, whose
                # deposit field is intentionally blank.
                ad = ad.model_copy(update={"deposit": None})
                apartment = await apartments.upsert_discovered(ad)
            except (LalafoError, LalafoNotFound, LalafoParseError, ValueError) as exc:
                # If Lalafo temporarily hides a phone or blocks the detail
                # route, an already verified database copy remains usable.
                apartment = await apartments.get_by_lalafo(item.lalafo_id)
                if apartment is None or not apartment.active or not apartment.phone:
                    logger.error(
                        "Selected apartment id=%s is unavailable: %s",
                        item.lalafo_id,
                        type(exc).__name__,
                    )
                    failures += 1
                    continue
                logger.warning(
                    "Using verified database copy for selected id=%s after %s",
                    item.lalafo_id,
                    type(exc).__name__,
                )

            assert apartment is not None
            card_source = ad if ad is not None else apartment
            try:
                message = await publisher.publish(apartment.id, card_source)
                await apartments.mark_published(
                    apartment.id,
                    chat_id=settings.telegram_group_id,
                    message_id=message.message_id,
                )
            except TelegramPublishError as exc:
                logger.error(
                    "Telegram delivery failed for selected id=%s: %s",
                    item.lalafo_id,
                    exc,
                )
                failures += 1
                continue
            published += 1
            logger.info(
                "SELECTED_PUBLISHED lalafo_id=%s apartment_id=%s message_id=%s photos=%s",
                item.lalafo_id,
                apartment.id,
                message.message_id,
                len(card_source.photo_urls),
            )
    finally:
        await client.close()
        await bot.session.close()
        await engine.dispose()

    logger.info("Selected Telegram publication finished: published=%d failed=%d", published, failures)
    return 0 if published == len(selected) and failures == 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

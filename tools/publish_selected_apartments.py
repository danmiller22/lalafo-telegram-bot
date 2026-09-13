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
from app.lalafo.exclusions import is_permanently_excluded
from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher


logger = logging.getLogger(__name__)
_LALAFO_ID = re.compile(r"-id-(\d+)(?:$|[/?#])")
SELECTED_REPOST_AFTER_HOURS = None
MANAGED_SELECTED_TERM_OVERRIDES = {
    116308426: (26_000, "Восток-5"),
    116308347: (40_000, "Восток-5"),
}


@dataclass(frozen=True)
class SelectedListing:
    lalafo_id: int
    url: str


def selected_managed_ad_ids(raw: str) -> set[int]:
    """Parse an explicit allow-list of active ads from the operator's profile."""
    result: set[int] = set()
    for value in re.split(r"[\s,]+", raw.strip()):
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"Unsupported managed Lalafo ID: {value}")
        result.add(int(value))
    return result


def stored_owner_ad(apartment, *, price: int, district: str | None) -> LalafoAd:
    """Use current public terms without replacing the verified source contact."""
    return LalafoAd(
        lalafo_id=apartment.lalafo_id,
        source_url=apartment.source_url,
        phone=apartment.phone,
        price=price,
        currency="KGS",
        rooms=apartment.rooms,
        district=district or apartment.district,
        city=apartment.city,
        deposit=None,
        photo_urls=list(apartment.photo_urls),
        category_id=2044,
        no_subletting=apartment.no_subletting,
        owner_listing=apartment.owner_listing,
    )


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
        if is_permanently_excluded(lalafo_id):
            raise ValueError(
                f"This Lalafo advertisement is permanently excluded: {lalafo_id}"
            )
        if lalafo_id in seen:
            continue
        seen.add(lalafo_id)
        result.append(SelectedListing(lalafo_id=lalafo_id, url=value))
    if not result:
        raise ValueError("SELECTED_LALAFO_URLS must contain at least one detail URL")
    if len(result) > 10:
        raise ValueError("A selected publication is limited to 10 apartments")
    return result


def eligible_selected_listings(
    selected: list[SelectedListing],
    published_ids: set[int],
    repostable_ids: set[int],
) -> tuple[list[SelectedListing], list[SelectedListing]]:
    """Keep never-published cards and report permanent duplicates separately."""
    eligible = [
        item
        for item in selected
        if item.lalafo_id not in published_ids
    ]
    eligible_ids = {item.lalafo_id for item in eligible}
    recent = [item for item in selected if item.lalafo_id not in eligible_ids]
    return eligible, recent


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
        managed_raw = os.getenv("SELECTED_MANAGED_LALAFO_AD_IDS", "").strip()
        managed_ad_ids = (
            selected_managed_ad_ids(managed_raw)
            if managed_raw
            else set(MANAGED_SELECTED_TERM_OVERRIDES)
        )
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
    skipped_recent = 0
    skipped_identity_duplicate = 0
    try:
        await init_db(engine)
        managed_by_source_id = {}
        if managed_ad_ids:
            managed_sources = await apartments.managed_lalafo_sources()
            selected_source_ids = {item.lalafo_id for item in selected}
            for managed in managed_sources:
                if managed.managed_lalafo_ad_id not in managed_ad_ids:
                    continue
                source = managed.apartment
                if (
                    source.phone_source_version != PHONE_SOURCE_VERSION
                    or not source.phone
                    or len(source.photo_urls) < 2
                ):
                    logger.error(
                        "Managed source id=%s has no verified original contact/photos",
                        source.lalafo_id,
                    )
                    failures += 1
                    continue
                managed_by_source_id[source.lalafo_id] = managed
                if source.lalafo_id not in selected_source_ids:
                    selected.append(
                        SelectedListing(
                            lalafo_id=source.lalafo_id,
                            url=source.source_url,
                        )
                    )
                    selected_source_ids.add(source.lalafo_id)
            missing_managed_ids = managed_ad_ids - {
                item.managed_lalafo_ad_id
                for item in managed_by_source_id.values()
                if item.managed_lalafo_ad_id is not None
            }
            for managed_id in sorted(missing_managed_ids):
                logger.error(
                    "Active managed ad id=%s has no verified original mapping",
                    managed_id,
                )
                failures += 1
        selected_ids = [item.lalafo_id for item in selected]
        published_ids = await apartments.published_lalafo_ids(selected_ids)
        selected, recent = eligible_selected_listings(
            selected,
            published_ids,
            set(),
        )
        skipped_recent = len(recent)
        for item in recent:
            logger.info(
                "SELECTED_SKIPPED_PERMANENT_DUPLICATE lalafo_id=%s",
                item.lalafo_id,
            )
        for item in selected:
            apartment = None
            ad: LalafoAd | None = None
            managed = managed_by_source_id.get(item.lalafo_id)
            try:
                if managed is None:
                    ad = await client.detail(item.url)
                else:
                    managed_ad = None
                    if managed.managed_lalafo_ad_url:
                        try:
                            managed_ad = await client.detail(
                                managed.managed_lalafo_ad_url
                            )
                        except (LalafoError, LalafoParseError, ValueError):
                            logger.info(
                                "Using account-visible terms for managed ad id=%s",
                                managed.managed_lalafo_ad_id,
                            )
                    override = MANAGED_SELECTED_TERM_OVERRIDES.get(
                        managed.managed_lalafo_ad_id or 0
                    )
                    if managed_ad is None and override is None:
                        raise LalafoParseError("Managed profile terms are unavailable")
                    ad = stored_owner_ad(
                        managed.apartment,
                        price=(override[0] if override else managed_ad.price),
                        district=(override[1] if override else managed_ad.district),
                    )
                if ad.currency.upper() != "KGS":
                    logger.error(
                        "Selected apartment id=%s has unsupported currency=%s",
                        item.lalafo_id,
                        ad.currency,
                    )
                    failures += 1
                    continue
                # These curated cards mirror the user's Lalafo drafts, whose
                # deposit field is intentionally blank.
                ad = ad.model_copy(update={"deposit": None})
                apartment = await apartments.upsert_discovered(ad)
            except (LalafoError, LalafoNotFound, LalafoParseError, ValueError) as exc:
                if managed is not None:
                    logger.error(
                        "Managed original id=%s is unavailable: %s",
                        item.lalafo_id,
                        type(exc).__name__,
                    )
                    failures += 1
                    continue
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
            if await apartments.is_duplicate(card_source):
                skipped_identity_duplicate += 1
                logger.info(
                    "SELECTED_SKIPPED_IDENTITY_DUPLICATE lalafo_id=%s",
                    item.lalafo_id,
                )
                continue
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

    logger.info(
        "Selected Telegram publication finished: published=%d skipped_recent=%d "
        "skipped_identity_duplicate=%d failed=%d",
        published,
        skipped_recent,
        skipped_identity_duplicate,
        failures,
    )
    handled = published + skipped_identity_duplicate
    return 0 if handled == len(selected) and failures == 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

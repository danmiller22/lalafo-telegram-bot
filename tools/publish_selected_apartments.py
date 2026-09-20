from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.exclusions import is_permanently_excluded
from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository, ManagedLalafoSource
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment
from app.telegram.keyboards import apartment_keyboard
from app.telegram.publisher import TelegramPublishError, TelegramPublisher


logger = logging.getLogger(__name__)
_LALAFO_ID = re.compile(r"-id-(\d+)(?:$|[/?#])")
SELECTED_REPOST_AFTER_HOURS = None
MANAGED_SELECTED_TERM_OVERRIDES = {
    114740296: (28_000, "Восток-5 мкр"),
    111730695: (32_000, "Филармония"),
    116490287: (22_000, "Филармония"),
    116490159: (28_000, "Восток-5 мкр"),
    116308426: (26_000, "Восток-5"),
    116308347: (28_000, "Восток-5"),
    116325992: (32_000, "Филармония"),
    116325997: (32_000, "Восток-5"),
    114621485: (35_000, "ЦУМ"),
}
MANAGED_KNOWN_SOURCE_IDS = {
    116308347: 116243546,
    116325992: 116308607,
    116325997: 114595809,
    114621485: 81141886,
}
MANAGED_KNOWN_PHOTO_URLS = {
    114740296: [
        "https://img5.lalafo.com/i/posters/api/7b/d6/f4/607bfc91bb9ac049678f820ae0.jpeg",
        "https://img5.lalafo.com/i/posters/api/df/25/f2/c4acb5b116dfa40e04aa3e374b.jpeg",
        "https://img5.lalafo.com/i/posters/api/f2/89/f6/266da6227c4f7e75481c33f6e3.jpeg",
        "https://img5.lalafo.com/i/posters/api/cd/f2/9f/86b5c6c2dff4d6ab71e0435b19.jpeg",
        "https://img5.lalafo.com/i/posters/api/14/3e/de/a37a9f5b9ad2a3a5244a0faa97.jpeg",
    ],
    111730695: [
        "https://img5.lalafo.com/i/posters/api/ff/03/87/e53abcb56810b3de74e4715e45.jpeg",
        "https://img5.lalafo.com/i/posters/api/b8/1e/31/5c50c36aeb07b5f7a2b87acf24.jpeg",
        "https://img5.lalafo.com/i/posters/api/97/90/87/406e0164b08808e299e4d05c3b.jpeg",
        "https://img5.lalafo.com/i/posters/api/4e/14/7a/348afb7ed50ef2a666ca47c01e.jpeg",
    ],
    116490287: [
        "https://img5.lalafo.com/i/posters/api/49/fd/83/b96a7271adaa387f4fed92002a.jpeg",
        "https://img5.lalafo.com/i/posters/api/1f/6d/19/8c9c48a1a4e4d1211dc497d40a.jpeg",
        "https://img5.lalafo.com/i/posters/api/44/84/9c/7b70fe773cd8978b2984d27eda.jpeg",
        "https://img5.lalafo.com/i/posters/api/4c/dd/f1/a99a3cc31ea426d99ae879688a.jpeg",
        "https://img5.lalafo.com/i/posters/api/2a/46/f7/98b61b2ec3ee3eb8832d0f4289.jpeg",
        "https://img5.lalafo.com/i/posters/api/bc/fa/bc/a877c7c1a95751b3af26a088ad.jpeg",
        "https://img5.lalafo.com/i/posters/api/f0/e7/03/f880ec87fea2a4243a844359ce.jpeg",
    ],
    116490159: [
        "https://img5.lalafo.com/i/posters/api/9d/1a/dd/a5e97d165aeffadaeaba90d014.jpeg",
        "https://img5.lalafo.com/i/posters/api/e4/13/fc/486d13f7a5bd3def6a9abf5301.jpeg",
        "https://img5.lalafo.com/i/posters/api/34/a4/73/779c671feeee8297b1ae15a527.jpeg",
        "https://img5.lalafo.com/i/posters/api/82/19/58/0155a9a9d5be8196875cd983bf.jpeg",
        "https://img5.lalafo.com/i/posters/api/36/de/2b/39c4b2908eab1e2137eca7f3a3.jpeg",
        "https://img5.lalafo.com/i/posters/api/5a/68/cd/d1942c394e2ec6cb244b35c02d.jpeg",
    ],
    116308426: [
        "https://img5.lalafo.com/i/posters/original/b5/40/1b/4a0b6ae6398e737c849fce2a97.jpeg",
        "https://img5.lalafo.com/i/posters/api/17/07/fc/14407976884fc70d579cc9e385.jpeg",
        "https://img5.lalafo.com/i/posters/api/46/8b/a7/2c0fbda0bebbf14c7d9a2739d3.jpeg",
        "https://img5.lalafo.com/i/posters/api/78/8a/79/f668f24e721f227272d0b60a8a.jpeg",
        "https://img5.lalafo.com/i/posters/api/c8/b0/f8/6da47fc20897b117166fe2907d.jpeg",
        "https://img5.lalafo.com/i/posters/api/c2/8a/89/9916e0c55c4c6f053f4d491139.jpeg",
    ],
}
SELECTED_CARD_CORRECTIONS = {
    114595809: (32_000, "Восток-5"),
    116352866: (21_000, "ЦУМ"),
    113286525: (32_000, "ЦУМ"),
}
SEARCH_REQUEST_ANNOUNCEMENT = (
    "🔎 Заполните заявку на поиск квартиры через нашего бота "
    "<b>@arenda312bot</b>."
)


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
        if item.lalafo_id not in published_ids or item.lalafo_id in repostable_ids
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


def _force_repost(raw_urls: str) -> bool:
    enabled_by_environment = os.getenv(
        "FORCE_SELECTED_REPOST", ""
    ).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if enabled_by_environment:
        return True
    return any(
        parse_qs(urlsplit(value).query).get("codex_force_repost") == ["1"]
        for value in re.split(r"[\s,]+", raw_urls.strip())
        if value
    )


def _search_request_announcement_requested(raw_urls: str) -> bool:
    return any(
        parse_qs(urlsplit(value).query).get("codex_announcement")
        == ["search_request"]
        for value in re.split(r"[\s,]+", raw_urls.strip())
        if value
    )


def _search_request_announcement_edit_message_id(raw_urls: str) -> int | None:
    for value in re.split(r"[\s,]+", raw_urls.strip()):
        if not value:
            continue
        raw_message_id = parse_qs(urlsplit(value).query).get(
            "codex_announcement_edit_message_id"
        )
        if raw_message_id:
            return int(raw_message_id[0])
    return None


async def _publish_search_request_announcement(
    bot: Bot,
    chat_id: int,
    *,
    edit_message_id: int | None = None,
) -> int:
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Заполнить заявку",
                    url="https://t.me/arenda312bot",
                )
            ]
        ]
    )
    if edit_message_id is not None:
        try:
            message = await bot.edit_message_text(
                SEARCH_REQUEST_ANNOUNCEMENT,
                chat_id=chat_id,
                message_id=edit_message_id,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if "message to edit not found" not in str(exc).lower():
                raise
            logger.warning(
                "Announcement message %s no longer exists; publishing a new one",
                edit_message_id,
            )
            message = await bot.send_message(
                chat_id,
                SEARCH_REQUEST_ANNOUNCEMENT,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
    else:
        message = await bot.send_message(
            chat_id,
            SEARCH_REQUEST_ANNOUNCEMENT,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    await bot.pin_chat_message(
        chat_id,
        message.message_id,
        disable_notification=False,
    )
    return message.message_id


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

    raw_urls = os.getenv("SELECTED_LALAFO_URLS", "")
    try:
        selected = selected_listings(raw_urls)
        managed_raw = os.getenv("SELECTED_MANAGED_LALAFO_AD_IDS", "").strip()
        managed_ad_ids = (
            selected_managed_ad_ids(managed_raw)
            if managed_raw
            else {
                item.lalafo_id
                for item in selected
                if item.lalafo_id in MANAGED_SELECTED_TERM_OVERRIDES
            }
        )
        selected_managed_ids = {
            item.lalafo_id for item in selected if item.lalafo_id in managed_ad_ids
        }
        if selected_managed_ids:
            managed_ad_ids = selected_managed_ids
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
    if _search_request_announcement_requested(raw_urls):
        try:
            message_id = await _publish_search_request_announcement(
                bot,
                settings.telegram_group_id,
                edit_message_id=_search_request_announcement_edit_message_id(
                    raw_urls
                ),
            )
            logger.info(
                "SEARCH_REQUEST_ANNOUNCEMENT_PUBLISHED message_id=%s pinned=true",
                message_id,
            )
            return 0
        finally:
            await bot.session.close()
            await engine.dispose()
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
            for managed in managed_sources:
                logger.info(
                    "MANAGED_SOURCE_MAPPING managed_id=%s source_lalafo_id=%s",
                    managed.managed_lalafo_ad_id,
                    managed.apartment.lalafo_id,
                )
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
                managed_url = (
                    "https://lalafo.kg/bishkek/ads/"
                    f"managed-id-{managed_id}"
                )
                source = None
                known_source_id = MANAGED_KNOWN_SOURCE_IDS.get(managed_id)
                if known_source_id is not None:
                    candidate = await apartments.get_by_lalafo(known_source_id)
                    if (
                        candidate is not None
                        and candidate.phone
                        and candidate.phone_source_version == PHONE_SOURCE_VERSION
                        and len(candidate.photo_urls) >= 2
                    ):
                        source = candidate
                if source is None:
                    managed_photo_urls = MANAGED_KNOWN_PHOTO_URLS.get(managed_id)
                    try:
                        managed_ad = await client.detail(managed_url)
                    except (LalafoError, LalafoParseError, ValueError) as exc:
                        if managed_photo_urls is None:
                            logger.warning(
                                "Active managed ad id=%s cannot be inspected: %s",
                                managed_id,
                                type(exc).__name__,
                            )
                            continue
                        logger.info(
                            "Using account-observed photos for managed ad id=%s",
                            managed_id,
                        )
                    else:
                        managed_photo_urls = list(managed_ad.photo_urls)
                    source = await apartments.verified_source_by_photos(
                        managed_photo_urls,
                        excluded_lalafo_ids=managed_ad_ids,
                    )
                if source is None:
                    logger.warning(
                        "Active managed ad id=%s has no unique verified photo match",
                        managed_id,
                    )
                    continue
                await apartments.save_managed_lalafo_source(
                    managed_lalafo_ad_id=managed_id,
                    managed_lalafo_ad_url=managed_url,
                    apartment=source,
                )
                managed = ManagedLalafoSource(
                    apartment=source,
                    managed_lalafo_ad_id=managed_id,
                    managed_lalafo_ad_url=managed_url,
                )
                managed_by_source_id[source.lalafo_id] = managed
                logger.info(
                    "MANAGED_SOURCE_MATCHED managed_id=%s source_lalafo_id=%s",
                    managed_id,
                    source.lalafo_id,
                )
                if source.lalafo_id not in selected_source_ids:
                    selected.append(
                        SelectedListing(
                            lalafo_id=source.lalafo_id,
                            url=source.source_url,
                        )
                    )
                    selected_source_ids.add(source.lalafo_id)
        selected = [
            item for item in selected if item.lalafo_id not in managed_ad_ids
        ]
        selected_ids = [item.lalafo_id for item in selected]
        published_ids = await apartments.published_lalafo_ids(selected_ids)
        for lalafo_id, (price, district) in SELECTED_CARD_CORRECTIONS.items():
            if lalafo_id not in selected_ids or lalafo_id not in published_ids:
                continue
            published_apartment = await apartments.get_by_lalafo(lalafo_id)
            if (
                published_apartment is None
                or not published_apartment.telegram_message_id
            ):
                continue
            corrected_ad = stored_owner_ad(
                published_apartment,
                price=price,
                district=district,
            )
            published_apartment = await apartments.upsert_discovered(corrected_ad)
            try:
                await bot.edit_message_text(
                    chat_id=settings.telegram_group_id,
                    message_id=published_apartment.telegram_message_id,
                    text=format_public_apartment(
                        corrected_ad,
                        bot_username=settings.telegram_bot_username,
                    ),
                    reply_markup=apartment_keyboard(
                        published_apartment.id,
                        signer=TokenSigner(callback_secret),
                        bot_username=settings.telegram_bot_username,
                        support_url=settings.support_url,
                    ),
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    raise
                logger.info(
                    "SELECTED_DISTRICT_ALREADY_CURRENT lalafo_id=%s district=%s",
                    lalafo_id,
                    district,
                )
            logger.info(
                "SELECTED_DISTRICT_CORRECTED lalafo_id=%s district=%s",
                lalafo_id,
                district,
            )
        force_repost = _force_repost(os.getenv("SELECTED_LALAFO_URLS", ""))
        selected, recent = eligible_selected_listings(
            selected,
            published_ids if not force_repost else set(),
            set(managed_by_source_id),
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
                    correction = SELECTED_CARD_CORRECTIONS.get(item.lalafo_id)
                    if correction is not None:
                        ad = ad.model_copy(
                            update={
                                "price": correction[0],
                                "district": correction[1],
                            }
                        )
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
            if (
                not force_repost
                and managed is None
                and await apartments.is_duplicate(card_source)
            ):
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


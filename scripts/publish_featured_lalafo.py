from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.featured.posting import posting_payload
from app.featured.campaigns import available_balance, campaign_identity, price_id_for_daily_budget
from app.featured.repository import FeaturedRepository
from app.featured.selection import build_description, select_featured
from app.featured.telegram import apartment_to_ad
from app.lalafo.client import LalafoClient
from app.lalafo.managed_ads import (
    LalafoManagedAdsClient,
    ManagedAdsAmbiguousResultError,
    ManagedAdsError,
    publication_status,
)
from app.lalafo.models import LalafoAd
from app.lalafo.parser import is_allowed
from app.models import FeaturedCandidate
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher

logger = logging.getLogger(__name__)


async def discover(settings) -> list[LalafoAd]:
    candidates: list[LalafoAd] = []
    async with LalafoClient(
        timeout=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        proxy_url=settings.lalafo_proxy_url,
    ) as client:
        for page_number in range(1, min(settings.max_search_pages, 10) + 1):
            page = await client.search(settings.lalafo_search_url, page=page_number)
            if not page.items:
                break
            for item in page.items:
                if len(candidates) >= max(40, settings.featured_max_candidates * 5):
                    return candidates
                if item.price and item.price > settings.featured_max_price:
                    continue
                try:
                    ad = await client.detail(item.detail_url)
                except Exception as exc:
                    logger.warning("Skipping source id=%s: %s", item.lalafo_id, type(exc).__name__)
                    continue
                allowed, _ = is_allowed(
                    ad, city=settings.city, max_price=settings.featured_max_price,
                    rooms=settings.allowed_rooms,
                )
                if allowed and ad.district and ad.phone and ad.photo_urls:
                    candidates.append(ad)
            if page_number >= page.page_count:
                break
    return candidates


def extract_id(payload: dict, *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


async def notify(bot: Bot | None, admin_id: int, text: str) -> bool:
    if bot is not None and admin_id:
        try:
            await bot.send_message(admin_id, text[:4000])
            return True
        except Exception:
            logger.exception("Could not send featured publication report")
    return False


async def emit_event_notifications(
    repo: FeaturedRepository,
    apartments: ApartmentRepository,
    bot: Bot,
    admin_id: int,
) -> None:
    """Send only durable, one-time lifecycle notifications."""
    now = datetime.now(timezone.utc)

    async def label(row) -> str:
        apartment = (
            await apartments.get(row.source_apartment_id)
            if row.source_apartment_id is not None
            else None
        )
        if apartment is None:
            return f"объявление ID {row.managed_lalafo_ad_id}"
        return f"{apartment.district or apartment.city}, {apartment.price} сом"

    for row in await repo.pending_new_notifications():
        message = (
            f"🆕 Новая реклама опубликована\n"
            f"🏠 {await label(row)}\n"
            f"Lalafo: {row.managed_lalafo_ad_url}\n"
            f"Telegram: сообщение {row.telegram_message_id}"
        )
        if await notify(bot, admin_id, message):
            await repo.patch(row.id, new_ad_notified_at=now)

    for row in await repo.expiring_soon(now):
        message = (
            f"⏳ До деактивации рекламы осталось около 2 часов\n"
            f"🏠 {await label(row)}\n"
            f"Lalafo: {row.managed_lalafo_ad_url}"
        )
        if await notify(bot, admin_id, message):
            await repo.patch(row.id, expiring_notified_at=now)

    for row in await repo.pending_deactivation_notifications():
        message = (
            f"⛔ Реклама деактивирована\n"
            f"🏠 {await label(row)}\n"
            f"Lalafo: {row.managed_lalafo_ad_url}"
        )
        if await notify(bot, admin_id, message):
            await repo.patch(row.id, deactivated_notified_at=now)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.dry_run and not settings.featured_publish_approved_enabled:
        logger.info("Approved featured publication disabled; no-op")
        return 0
    started = datetime.now(timezone.utc)
    business_date = started.astimezone(ZoneInfo(settings.featured_timezone)).date()
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    repo = FeaturedRepository(sessions)
    apartments = ApartmentRepository(sessions)
    async with repo.run_lock() as acquired:
        if not acquired:
            logger.info("Another daily featured run owns the lock; no-op")
            await engine.dispose()
            return 0
        auto_approved = await repo.approve_custom_candidates(business_date)
        if auto_approved:
            logger.info("Auto-approved admin links queued before immediate mode: %d", auto_approved)
        approved = await repo.selected_candidates(business_date)
        if not approved:
            logger.info("No approved featured apartments; no-op")
            await engine.dispose()
            return 0
        selected: list[tuple[FeaturedCandidate, LalafoAd]] = []
        for candidate in approved[: settings.featured_count]:
            if candidate.source_apartment_id is not None:
                apartment = await apartments.get(candidate.source_apartment_id)
                if apartment is not None:
                    selected.append((candidate, apartment_to_ad(apartment)))
                    continue
            ad = LalafoAd.model_validate(candidate.source_payload)
            apartment = await apartments.upsert_discovered(ad)
            await repo.bind_candidate_apartment(candidate.id, apartment.id)
            selected.append((candidate, ad))
        if settings.dry_run:
            for candidate, ad in selected:
                logger.info(
                    "DRY approved slot=%d id=%s url=%s\n%s",
                    candidate.selected_slot, ad.lalafo_id, ad.source_url,
                    build_description(ad),
                )
            await engine.dispose()
            return 0
        bot = Bot(token=settings.require_bot_token())
        report_bot = (
            Bot(token=settings.featured_review_bot_token)
            if settings.featured_review_bot_token else bot
        )
        publisher = TelegramPublisher(
            bot, chat_id=settings.telegram_group_id,
            signer=TokenSigner(settings.require_callback_secret()),
            bot_username=settings.telegram_bot_username, support_url=settings.support_url,
            max_photos=settings.featured_max_photos,
        )
        managed = LalafoManagedAdsClient(timeout=settings.http_timeout_seconds)
        report: list[str] = []
        promotion_blocked = False
        try:
            login, password = settings.require_lalafo_auto_reply_credentials()
            await managed.login(login, password)
            for old in await repo.previous_active(business_date):
                try:
                    if old.campaign_id and old.campaign_status not in {"cancelled", "stopped"}:
                        await managed.cancel_campaign(old.campaign_id)
                        await repo.patch(old.id, campaign_status="cancelled")
                    await managed.deactivate([int(old.managed_lalafo_ad_id)])
                    await repo.patch(
                        old.id, lalafo_publication_status="deactivated",
                        deactivated_at=datetime.now(timezone.utc),
                    )
                except ManagedAdsError:
                    promotion_blocked = True
                    await repo.patch(
                        old.id, last_error="Could not verify previous campaign deactivation"
                    )
            had_error = False
            for candidate, ad in selected:
                requested_slot = candidate.selected_slot or 1
                apartment = await apartments.upsert_discovered(ad)
                row = await repo.reserve(
                    business_date, requested_slot, apartment.id, ad.lalafo_id
                )
                try:
                    changed = False
                    if (
                        row.managed_lalafo_ad_id is None
                        and row.lalafo_publication_status == "unknown"
                    ):
                        had_error = True
                        report.append(
                            f"⚠️ {ad.district}: результат прошлой публикации неизвестен; "
                            "автоповтор заблокирован, чтобы не создать дубль"
                        )
                        continue
                    if row.managed_lalafo_ad_id is None:
                        temp_id = row.managed_lalafo_temp_id
                        if temp_id is None:
                            temp = await managed.create_temp()
                            temp_id = extract_id(temp, "id", "ad", "data")
                            if temp_id is None:
                                raise ManagedAdsError("Temporary ad response has no id")
                            row = await repo.patch(
                                row.id, managed_lalafo_temp_id=temp_id,
                                lalafo_publication_status="draft_created",
                                last_error=None,
                            )
                            changed = True
                        payload = posting_payload(ad)
                        payload["id"] = temp_id
                        if row.lalafo_publication_status == "draft_created":
                            await managed.update_temp(temp_id, payload)
                            row = await repo.patch(
                                row.id, lalafo_publication_status="draft_filled",
                                last_error=None,
                            )
                        photos = ad.photo_urls[: settings.featured_max_photos]
                        for photo in photos[row.managed_lalafo_uploaded_photos:]:
                            await managed.upload_image(temp_id, photo)
                            row = await repo.patch(
                                row.id,
                                managed_lalafo_uploaded_photos=(
                                    row.managed_lalafo_uploaded_photos + 1
                                ),
                                last_error=None,
                            )
                        row = await repo.patch(
                            row.id, lalafo_publication_status="draft_ready",
                            last_error=None,
                        )
                        try:
                            published = await managed.publish_temp(temp_id)
                        except ManagedAdsAmbiguousResultError as exc:
                            await repo.patch(
                                row.id, lalafo_publication_status="unknown",
                                last_error=(
                                    "Publish outcome unknown: " + type(exc).__name__
                                ),
                            )
                            raise
                        ad_id = extract_id(published, "id", "ad", "data")
                        if ad_id is None:
                            await repo.patch(
                                row.id, lalafo_publication_status="unknown",
                                last_error="Publish response has no verifiable ad id",
                            )
                            raise ManagedAdsAmbiguousResultError(
                                "Published ad response has no id"
                            )
                        row = await repo.patch(
                            row.id, managed_lalafo_ad_id=ad_id,
                            managed_lalafo_ad_url=f"https://lalafo.kg/bishkek/ads/id-{ad_id}",
                            lalafo_publication_status="created", last_error=None,
                        )
                        changed = True
                    ad_id = int(row.managed_lalafo_ad_id)
                    details = await managed.my_ad_details(ad_id)
                    visible_status = publication_status(details)
                    if visible_status != row.lalafo_publication_status:
                        error = None
                        if visible_status != "active":
                            error = (
                                "Lalafo created the ad but its owner status is "
                                f"{visible_status}; paid activation is disabled"
                            )
                        row = await repo.patch(
                            row.id,
                            lalafo_publication_status=visible_status,
                            last_error=error,
                        )
                        changed = True
                    if row.telegram_message_id is None:
                        message = await publisher.publish(apartment.id, ad)
                        await apartments.mark_published(
                            apartment.id, chat_id=settings.telegram_group_id,
                            message_id=message.message_id,
                        )
                        row = await repo.patch(
                            row.id, telegram_message_id=message.message_id,
                            telegram_chat_id=settings.telegram_group_id,
                        )
                        changed = True
                    if changed:
                        logger.info(
                            "Featured ad id=%s status=%s telegram_message=%s",
                            row.managed_lalafo_ad_id,
                            row.lalafo_publication_status,
                            row.telegram_message_id,
                        )
                except Exception as exc:
                    had_error = True
                    logger.exception(
                        "Featured source id=%s failed safely", ad.lalafo_id
                    )
                    if row.lalafo_publication_status != "unknown":
                        await repo.patch(
                            row.id, last_error=type(exc).__name__
                        )
                    report.append(
                        f"❌ {ad.district}, {ad.price} сом: {type(exc).__name__}"
                    )
            committed = await repo.daily_committed_budget(business_date)
            if settings.featured_autopromote_enabled:
                if promotion_blocked or committed >= settings.featured_max_daily_budget:
                    report.append("Реклама заблокирована защитой бюджета/старой кампании")
                elif len(await repo.for_date(business_date)) < settings.featured_count:
                    report.append("Реклама не запущена: создано менее двух объявлений")
                else:
                    balances = await managed.wallet_balances()
                    balance = available_balance(balances)
                    if balance is None or balance < settings.featured_min_wallet_balance:
                        report.append(
                            f"Недостаточно средств: баланс {balance}, нужно "
                            f"{settings.featured_min_wallet_balance} сом"
                        )
                    else:
                        for row in await repo.for_date(business_date):
                            if row.campaign_id:
                                continue
                            ad_id = int(row.managed_lalafo_ad_id)
                            stats = await managed.campaign_stats(ad_id)
                            existing_id, existing_status = campaign_identity(stats)
                            if existing_id:
                                await repo.patch(
                                    row.id, campaign_id=existing_id,
                                    campaign_status=existing_status or "active",
                                )
                                continue
                            reserved = await repo.reserve_campaign_budget(
                                row.id, amount=settings.featured_daily_budget_per_ad,
                                daily_limit=settings.featured_max_daily_budget,
                            )
                            if not reserved:
                                continue
                            params = await managed.campaign_params(ad_id)
                            price_id = price_id_for_daily_budget(
                                params, settings.featured_daily_budget_per_ad
                            )
                            try:
                                result = await managed.start_campaign(ad_id, price_id)
                            except Exception:
                                # An ambiguous timeout may happen after charging. Never
                                # retry until a read confirms whether a campaign exists.
                                stats = await managed.campaign_stats(ad_id)
                                campaign_id, status = campaign_identity(stats)
                                if campaign_id:
                                    await repo.patch(
                                        row.id, campaign_id=campaign_id,
                                        campaign_status=status or "active",
                                    )
                                    continue
                                await repo.patch(
                                    row.id, campaign_status="unknown",
                                    last_error="Campaign payment outcome is unknown",
                                )
                                promotion_blocked = True
                                break
                            campaign_id, status = campaign_identity(result)
                            if not campaign_id:
                                stats = await managed.campaign_stats(ad_id)
                                campaign_id, status = campaign_identity(stats)
                            if not campaign_id:
                                await repo.patch(
                                    row.id, campaign_status="unknown",
                                    last_error="Campaign response has no verifiable id",
                                )
                                promotion_blocked = True
                                break
                            await repo.patch(
                                row.id, campaign_id=campaign_id,
                                campaign_status=status or "active",
                            )
                        report.append(
                            f"Зарезервированный рекламный бюджет: "
                            f"{await repo.daily_committed_budget(business_date)} сом"
                        )
            for line in report:
                logger.info("Featured publication note: %s", line)
            await emit_event_notifications(
                repo, apartments, report_bot, settings.admin_user_id
            )
            if had_error:
                return 2
        except Exception as exc:
            logger.exception("Daily featured run failed safely")
            return 2
        finally:
            await managed.close()
            await bot.session.close()
            if report_bot is not bot:
                await report_bot.session.close()
            await engine.dispose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

"""Publish fresh account snapshots once per four-hour cycle, with durable receipts."""
import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import PHONE_SOURCE_VERSION
from app.managed_cards import PREFIX
from app.models import LalafoAutoReplyMeta, RequestedCardPromotion, ApartmentInventoryQueue
from app.promotions import external_repeat_receipt_key, REQUESTED_WINDOW_PREFIX, record_delivery
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from app.inventory import is_excluded_contact
from scripts.scrape_publish import apartment_to_ad, is_substandard_structure
from tools.lalafo_campaign_session import load_session

PROFILE = 18225388
CHAT = -1004389602150


def validate_packet(data, now):
    checked = datetime.fromisoformat(data['verified_at'])
    if not timedelta(0) <= now - checked <= timedelta(minutes=45):
        raise ValueError('stale_account_snapshot')
    if data['profile_id'] != PROFILE or data['cycle'] != int(checked.timestamp()) // 14400:
        raise ValueError('invalid_profile_or_cycle')
    cards = data['cards']
    if len(cards) > 100 or len({c['id'] for c in cards}) != len(cards):
        raise ValueError('duplicate_or_oversized_batch')
    for card in cards:
        if (card['user_id'] != PROFILE or card['status_id'] != 2
                or card['rooms'] not in {'1', '2', 'studio'} or card['price'] <= 0
                or not card['district'] or not card['photo_urls']
                or card['category_id'] != 2044 or card['city_id'] != 103184
                or str(card['currency']).upper() != 'KGS'
                or 'пример для подбора' in card['source_description'].casefold()
                or re.search(r'ориентир:?\s*\d', card['source_description'], re.I)):
            raise ValueError('invalid_active_apartment_snapshot')
    return cards


async def publish(data, sessions, publisher, now):
    cards = validate_packet(data, now)
    repo = ApartmentRepository(sessions)
    mappings = {r.managed_lalafo_ad_id: r.apartment
                for r in await repo.managed_lalafo_sources(deduplicate_sources=False)}
    # Historical explicit bindings may predate DailyFeaturedPublication records.
    async with sessions() as session:
        for marker in await session.scalars(select(LalafoAutoReplyMeta).where(
                LalafoAutoReplyMeta.key.startswith(PREFIX))):
            binding = json.loads(marker.value)
            if binding.get('profile_id') == PROFILE:
                ad_id = int(binding['ad_id'])
                if ad_id not in mappings:
                    original = await repo.get(int(marker.key[len(PREFIX):]))
                    if original is not None:
                        mappings[ad_id] = original
    resolved = []
    used = set()
    skipped = 0
    missing = 0
    for card in cards:
        original = mappings.get(card['id'])
        if original is None:
            original = await repo.verified_source_by_photos(card['photo_urls'],
                excluded_lalafo_ids={c['id'] for c in cards})
        if original is None or not original.phone or original.phone_source_version != PHONE_SOURCE_VERSION:
            print(f"SKIPPED_UNVERIFIED_SOURCE managed_id={card['id']}", flush=True)
            missing += 1
            continue
        ad = apartment_to_ad(original)
        if is_excluded_contact(ad):
            raise ValueError(f"excluded_source:{card['id']}")
        # Repeats cannot consume the limited general-feed non-owner allowance.
        # Preserve the author's actual classification; never relabel an agent.
        if ad.seller_type != 'owner' or not ad.owner_listing:
            print(f"SKIPPED_NON_OWNER managed_id={card['id']}", flush=True)
            skipped += 1
            continue
        if not ad.no_subletting or is_substandard_structure(ad):
            raise ValueError(f"ineligible_original:{card['id']}")
        if original.id in used:
            continue
        used.add(original.id)
        refreshed = ad.model_copy(update={k: card[k] for k in (
            'price', 'rooms', 'district', 'photo_urls', 'source_description')})
        refreshed = refreshed.model_copy(update={'deposit': None})
        resolved.append((card, original, refreshed))
    # Validate every original before reserving or sending any card.
    for card, original, ad in resolved:
        key = f"owned4h:{data['cycle']}:{card['id']}"
        async with sessions.begin() as session:
            marker = await session.get(LalafoAutoReplyMeta, key)
            if marker is not None:
                if marker.value.startswith('published:'):
                    print(f"ALREADY_PUBLISHED managed_id={card['id']} {marker.value}", flush=True)
                    continue
                raise RuntimeError(f"reserved_unverified:{card['id']}")
            session.add(LalafoAutoReplyMeta(key=key, value='reserved_unverified'))
        await repo.upsert_discovered(ad, discovery_priority=True)
        message = await publisher.publish(original.id, ad)
        await repo.mark_published(original.id, chat_id=CHAT, message_id=message.message_id,
                                  acknowledged_at=message.date, promote_requested=False)
        async with sessions.begin() as session:
            marker = await session.get(LalafoAutoReplyMeta, key)
            marker.value = f'published:{message.message_id}'
            await record_delivery(session, original, chat_id=CHAT, message_id=message.message_id, published_at=message.date)
            session.add(LalafoAutoReplyMeta(key=external_repeat_receipt_key(CHAT, message.message_id), value='4h'))
        print(f"PUBLISHED managed_id={card['id']} message_id={message.message_id}", flush=True)
    # The explicit four-hour schedule replaces older timed repeats of these
    # same account cards, without touching the ordinary discovery queue.
    ids = {original.id for _, original, _ in resolved}
    async with sessions.begin() as session:
        await session.execute(update(RequestedCardPromotion).where(
            RequestedCardPromotion.apartment_id.in_(ids)).values(enabled=False))
        await session.execute(update(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.apartment_id.in_(ids),
            ApartmentInventoryQueue.window_key.startswith(REQUESTED_WINDOW_PREFIX),
            ApartmentInventoryQueue.status == 'queued').values(status='skipped', last_error='external_four_hour_schedule'))
        for original_id in ids:
            binding = await session.get(LalafoAutoReplyMeta, PREFIX + str(original_id))
            if binding is not None:
                value = json.loads(binding.value)
                if value.get('profile_id') == PROFILE:
                    value.update(enabled=False, externally_scheduled=True, repeat_every_hours=4)
                    binding.value = json.dumps(value)
    print(f"FOURHOUR_COMPLETED eligible={len(resolved)} skipped_nonowners={skipped} missing_sources={missing}", flush=True)


async def main(path):
    settings = get_settings()
    if settings.telegram_group_id != CHAT:
        raise ValueError('unexpected_destination')
    envelope = json.loads(Path(path).read_text())
    data = await load_session(envelope['sealed_card'], key_name=envelope['key_name'])
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    publisher = TelegramPublisher(bot, chat_id=CHAT,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url)
    try:
        await publish(data, sessions, publisher, datetime.now(timezone.utc))
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--envelope', required=True)
    try:
        asyncio.run(main(parser.parse_args().envelope))
    except Exception as error:
        print(f'FOURHOUR_STOP {type(error).__name__}: {str(error) if isinstance(error, (ValueError, RuntimeError)) else "operation_failed"}', flush=True)
        raise SystemExit(1)

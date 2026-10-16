"""Repeat explicitly enrolled managed ads through the shared publication queue."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.inventory import (InventoryRepository, MAX_PUBLICATIONS_PER_DAY,
                           MIN_PUBLICATION_GAP_MINUTES, PUBLICATION_SPACING_MINUTES,
                           as_utc, publication_day_start, publication_window_open)
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.managed_cards import (PREFIX, ManagedCardUnavailable, managed_bindings,
                               refresh_managed_card, validate_binding)
from app.models import Apartment, ApartmentInventoryQueue, LalafoAutoReplyMeta, RequestedCardPromotion
from app.payments.repository import ApartmentRepository
from app.promotions import promotion_work, publications_in_window, register_requested
from scripts.publish_inventory import _publish_item
from scripts.scrape_publish import apartment_to_ad

LOOKAHEAD = timedelta(minutes=15)


def reservation_time(now, published_count, last_sent):
    if not publication_window_open(now) or published_count >= MAX_PUBLICATIONS_PER_DAY:
        return None
    start = publication_day_start(now).astimezone(timezone.utc)
    when = max(now, start + timedelta(minutes=PUBLICATION_SPACING_MINUTES * published_count))
    if last_sent is not None:
        when = max(when, as_utc(last_sent) + timedelta(minutes=MIN_PUBLICATION_GAP_MINUTES))
    return when if when - now <= LOOKAHEAD and publication_window_open(when) else None


async def enroll(sessions, manifest, *, now):
    """Enroll once; rerunning the same batch cannot extend or reset it."""
    key = 'managed_requested_batch:' + manifest['batch']
    repo = ApartmentRepository(sessions)
    resolved = []
    async with sessions() as session:
        existing = await session.get(LalafoAutoReplyMeta, key)
        if existing:
            print('MANAGED_BATCH_ALREADY_ENROLLED=' + existing.value, flush=True)
            return
    # Resolve actual existing contact cards; never synthesize a telephone.
    for row in manifest['cards']:
        validate_binding(row)
        if row['profile_id'] != 18194628:
            raise ValueError('Unexpected managed account')
        if row.get('source_id'):
            apartment = await repo.get_by_lalafo(row['source_id'])
        else:
            apartment = await repo.get_by_telegram_message(
                chat_id=-1004389602150, message_id=row['telegram_message_id'])
        if apartment is None or not apartment.telegram_message_id or not apartment.active:
            raise ValueError('Verified original Telegram card is missing')
        resolved.append((apartment, row))
    if not resolved or len(resolved) > 10 or len({a.id for a, _ in resolved}) != len(resolved):
        raise ValueError('Expected unique explicitly requested originals')
    async with sessions.begin() as session:
        if await session.get(LalafoAutoReplyMeta, key, with_for_update=True):
            return
        receipts = []
        for apartment, row in resolved:
            apartment = await session.get(Apartment, apartment.id)
            await register_requested(session, apartment, now=now)
            promotion = await session.get(RequestedCardPromotion, apartment.id)
            # Older web deployments must not refresh these curated cards from
            # a different original or send an empty-phone card as a paid contact.
            # New publishers opt in only through the verified managed binding.
            promotion.enabled = False
            await session.execute(update(ApartmentInventoryQueue).where(
                ApartmentInventoryQueue.apartment_id == apartment.id,
                ApartmentInventoryQueue.status == 'queued',
            ).values(status='skipped', last_error='managed_enrollment'))
            binding = {k: row[k] for k in ('profile_id', 'ad_id', 'url')}
            binding['enabled'] = True
            marker = await session.get(LalafoAutoReplyMeta, PREFIX + str(apartment.id))
            if marker is None:
                session.add(LalafoAutoReplyMeta(key=PREFIX + str(apartment.id), value=json.dumps(binding)))
            else:
                marker.value = json.dumps(binding)
            receipts.append({'apartment_id': apartment.id, 'managed_ad_id': row['ad_id'],
                             'expires_at': promotion.expires_at.isoformat()})
        value = json.dumps(receipts)
        session.add(LalafoAutoReplyMeta(key=key, value=value))
        print('MANAGED_BATCH_ENROLLED=' + value, flush=True)


async def run():
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    engine, sessions = create_engine_and_session(settings.database_url)
    inventory = InventoryRepository(sessions)
    repo = ApartmentRepository(sessions)
    item = None
    handed_to_publisher = False
    try:
        if os.environ.get('MANAGED_REPEAT_MANIFEST'):
            if os.environ.get('CONFIRM_MANAGED_REPEATS') != 'true':
                raise ValueError('Explicit enrollment confirmation required')
            manifest = json.loads(Path(os.environ['MANAGED_REPEAT_MANIFEST']).read_text())
            await enroll(sessions, manifest, now=datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        async with sessions() as session:
            bindings = await managed_bindings(session)
            if not bindings:
                return 0
            ids = set(await session.scalars(select(RequestedCardPromotion.apartment_id).where(
                RequestedCardPromotion.apartment_id.in_(bindings),
                RequestedCardPromotion.expires_at > now)))
            if not ids:
                print('MANAGED_REPEATS_EXPIRED', flush=True)
                return 0
            start = publication_day_start(now).astimezone(timezone.utc)
            events = await publications_in_window(session, start, start + timedelta(days=1))
            latest = max((as_utc(row.published_at) for row in events), default=None)
        when = reservation_time(now, len(events), latest)
        if when is None:
            print('MANAGED_REPEATS_WAITING_FOR_SLOT', flush=True)
            return 0
        async with sessions() as session:
            due, _ = await promotion_work(session, now=when, day_start=start,
                                         day_end=start + timedelta(hours=21))
            # Reserve only one managed row; leave no queued rows for an older
            # deployment to pick up and refresh through the original source.
            candidate = next((apartment.id for _, apartment in due if apartment.id in ids), None)
        if candidate is None:
            print('MANAGED_REPEATS_NOT_DUE', flush=True)
            return 0
        # The normal transactional claim is also a reservation. While it waits
        # for this one slot, every old/new publisher sees the shared in-flight
        # row. Source collection, payments and the bot remain independent.
        item = await inventory.claim_due(now=when, eligible_until=when,
                                         only_apartment_ids={candidate}, requested_only=True,
                                         reservation_clock=now)
        if item is None:
            print('MANAGED_REPEATS_NOT_DUE', flush=True)
            return 0
        print(f'MANAGED_REPEAT_RESERVED apartment_id={item.apartment_id} not_before={when.isoformat()}', flush=True)
        try:
            async with LalafoClient(timeout=15, max_retries=0) as client:
                await refresh_managed_card(client, bindings[item.apartment_id], apartment_to_ad(item.apartment))
        except (ManagedCardUnavailable, LalafoNotFound):
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, PREFIX + str(item.apartment_id))
                value = json.loads(marker.value)
                value['enabled'] = False
                marker.value = json.dumps(value)
            print('MANAGED_AD_NO_LONGER_ACTIVE', flush=True)
            return 0
        except (LalafoError, ValueError, TimeoutError) as exc:
            print(f'MANAGED_PUBLIC_CHECK_UNKNOWN type={type(exc).__name__}', flush=True)
            return 0
        while datetime.now(timezone.utc) < when:
            await asyncio.sleep(min(30, max(0, (when - datetime.now(timezone.utc)).total_seconds())))
        # Manual deliveries could have arrived while a slot was reserved.
        async with sessions() as session:
            start = publication_day_start(datetime.now(timezone.utc)).astimezone(timezone.utc)
            current = await publications_in_window(session, start, start + timedelta(days=1))
            latest = max((as_utc(row.published_at) for row in current), default=None)
        if len(current) >= MAX_PUBLICATIONS_PER_DAY or (latest and datetime.now(timezone.utc) - latest < timedelta(minutes=MIN_PUBLICATION_GAP_MINUTES)):
            await inventory.finish_item(item.id, status='skipped', error='managed_slot_taken')
            item = None
            return 0
        handed_to_publisher = True
        result = await _publish_item(settings, sessions, inventory, repo, item) or 0
        latest_card = await repo.get(item.apartment_id)
        if latest_card.published_at and as_utc(latest_card.published_at) >= when:
            print(f'MANAGED_REPEAT_PUBLISHED managed_id={bindings[item.apartment_id]["ad_id"]} '
                  f'message_id={latest_card.telegram_message_id}', flush=True)
        return result
    finally:
        if item is not None and not handed_to_publisher:
            await inventory.finish_item(item.id, status='skipped', error='managed_preflight_interrupted')
        await engine.dispose()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))

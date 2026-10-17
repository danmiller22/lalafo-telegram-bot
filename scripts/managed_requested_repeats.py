"""Reserve shared Telegram publication slots for fresh stock and managed repeats."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.inventory import (InventoryRepository, MAX_PUBLICATIONS_PER_DAY,
                           MIN_PUBLICATION_GAP_MINUTES, PUBLICATION_SPACING_MINUTES,
                           as_utc, publication_day_start, publication_slots_due,
                           publication_window_open)
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.managed_cards import (PREFIX, ManagedCardUnavailable, managed_bindings,
                               refresh_managed_card, validate_binding)
from app.models import Apartment, ApartmentInventoryQueue, LalafoAutoReplyMeta, RequestedCardPromotion
from app.payments.repository import ApartmentRepository
from app.promotions import promotion_work, publications_in_window, register_requested
from scripts.publish_inventory import _publish_item, run as publish_next_due
from scripts.scrape_publish import apartment_to_ad

LOOKAHEAD = timedelta(minutes=15)


async def latest_delivery_time(session, events):
    # Telegram timestamps have whole-second precision; the queue commits its
    # acknowledgement slightly later. Use the same clocks as claim_due so a
    # valid future reservation is not rejected by that sub-second difference.
    clocks = [as_utc(row.published_at) for row in events]
    queued = await session.scalar(select(func.max(ApartmentInventoryQueue.published_at)).where(
        ApartmentInventoryQueue.status == 'published'))
    if queued is not None:
        clocks.append(as_utc(queued))
    return max(clocks, default=None)


def reservation_gap(now, published_count, *, catch_up=False):
    return 5 if catch_up and publication_slots_due(now) - published_count >= 3 else MIN_PUBLICATION_GAP_MINUTES


def reservation_time(now, published_count, last_sent, *, catch_up=False):
    if not publication_window_open(now) or published_count >= MAX_PUBLICATIONS_PER_DAY:
        return None
    from app.inventory import next_publication_slot
    when = max(now, next_publication_slot(now, published_count, last_sent))
    if last_sent is not None:
        when = max(when, as_utc(last_sent) + timedelta(minutes=reservation_gap(now, published_count, catch_up=catch_up)))
    return when if when - now <= LOOKAHEAD and publication_window_open(when) else None


async def enroll(sessions, manifest, *, now):
    """Enroll once; rerunning the same batch cannot extend or reset it."""
    cadence = manifest.get('repeat_every_hours')
    if cadence is not None and cadence != 3:
        raise ValueError('Unsupported managed repeat interval')
    key = 'managed_requested_batch:' + manifest['batch']
    if len(key) > 64:
        raise ValueError('Managed batch identifier exceeds database key limit')
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
            if cadence is not None:
                binding['repeat_every_hours'] = cadence
                binding['repeat_while_active'] = manifest.get('repeat_while_active') is True
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
    all_cards = os.environ.get('QUEUE_RESERVATION_ALL_CARDS') == 'true'
    try:
        if os.environ.get('MANAGED_REPEAT_MANIFEST'):
            if os.environ.get('CONFIRM_MANAGED_REPEATS') != 'true':
                raise ValueError('Explicit enrollment confirmation required')
            manifest = json.loads(Path(os.environ['MANAGED_REPEAT_MANIFEST']).read_text())
            await enroll(sessions, manifest, now=datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        if all_cards:
            await inventory.schedule_period(now=now)
        async with sessions() as session:
            bindings = await managed_bindings(session)
            if not bindings and not all_cards:
                return 0
            ids = set(await session.scalars(select(RequestedCardPromotion.apartment_id).where(
                RequestedCardPromotion.apartment_id.in_(bindings),
                or_(RequestedCardPromotion.expires_at > now,
                    RequestedCardPromotion.apartment_id.in_([
                        key for key, value in bindings.items()
                        if value.get('repeat_every_hours') == 3 and value.get('repeat_while_active') is True])))))
            if not ids and not all_cards:
                print('MANAGED_REPEATS_EXPIRED', flush=True)
                return 0
            start = publication_day_start(now).astimezone(timezone.utc)
            events = await publications_in_window(session, start, start + timedelta(days=1))
            latest = await latest_delivery_time(session, events)
        when = reservation_time(now, len(events), latest, catch_up=True)
        if when is None:
            print('MANAGED_REPEATS_WAITING_FOR_SLOT', flush=True)
            return 0
        async with sessions() as session:
            due, _ = await promotion_work(session, now=when, day_start=start,
                                         day_end=start + timedelta(hours=21))
            # Reserve only one managed row; leave no queued rows for an older
            # deployment to pick up and refresh through the original source.
            candidate = next((apartment.id for _, apartment in due if apartment.id in ids), None)
        managed_candidate = candidate
        if all_cards and any(event.apartment_id in bindings for event in events[:2]):
            # Interleave requested repeats with two different ordinary cards
            # whenever fresh stock exists, rather than filling consecutive
            # slots with previously seen managed apartments.
            candidate = None
        if candidate is None and not all_cards:
            print('MANAGED_REPEATS_NOT_DUE', flush=True)
            return 0
        # The normal transactional claim is also a reservation. While it waits
        # for this one slot, every old/new publisher sees the shared in-flight
        # row. Source collection, payments and the bot remain independent.
        item = await inventory.claim_due(now=when, eligible_until=when,
                                         only_apartment_ids={candidate} if candidate is not None else None,
                                         requested_only=candidate is not None,
                                         reservation_clock=now)
        if item is None and candidate is None and managed_candidate is not None:
            # A fresh-stock shortage or reserved repeat quota must not leave
            # the channel empty when a requested repeat is due.
            item = await inventory.claim_due(now=when, eligible_until=when,
                only_apartment_ids={managed_candidate}, requested_only=True,
                reservation_clock=now)
        if item is None:
            print('MANAGED_REPEATS_NOT_DUE', flush=True)
            return 0
        print(f'MANAGED_REPEAT_RESERVED apartment_id={item.apartment_id} not_before={when.isoformat()}', flush=True)
        binding = bindings.get(item.apartment_id)
        try:
            if binding:
                async with LalafoClient(timeout=15, max_retries=0, proxy_url=settings.lalafo_proxy_url) as client:
                    await refresh_managed_card(client, binding, apartment_to_ad(item.apartment))
        except (ManagedCardUnavailable, LalafoNotFound) as exc:
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, PREFIX + str(item.apartment_id))
                value = json.loads(marker.value)
                value['enabled'] = False
                marker.value = json.dumps(value)
            print(f'MANAGED_AD_NO_LONGER_ACTIVE type={type(exc).__name__}', flush=True)
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
            latest = await latest_delivery_time(session, current)
        gap = reservation_gap(datetime.now(timezone.utc), len(current), catch_up=True)
        if len(current) >= MAX_PUBLICATIONS_PER_DAY or (latest and datetime.now(timezone.utc) - latest < timedelta(minutes=gap)):
            await inventory.finish_item(item.id, status='skipped', error='managed_slot_taken')
            item = None
            return 0
        handed_to_publisher = True
        result = await _publish_item(settings, sessions, inventory, repo, item)
        if result is None and all_cards:
            # A removed/rejected card must not waste the whole cloud tick.
            # The ordinary dispatcher skips terminal failures until one real
            # delivery, while retaining the same shared gap and daily cap.
            return await publish_next_due(sessions=sessions)
        latest_card = await repo.get(item.apartment_id)
        if latest_card.published_at and as_utc(latest_card.published_at) >= when:
            print(f'QUEUE_CARD_PUBLISHED apartment_id={item.apartment_id} '
                  f'managed_id={binding["ad_id"] if binding else "none"} '
                  f'message_id={latest_card.telegram_message_id}', flush=True)
        return result or 0
    finally:
        if item is not None and not handed_to_publisher:
            await inventory.finish_item(item.id, status='skipped', error='managed_preflight_interrupted')
        await engine.dispose()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))

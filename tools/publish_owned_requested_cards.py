"""Send an explicit owned batch with verified stored contacts and 3h repeats."""
import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import InputMediaPhoto, ReplyParameters
from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.managed_cards import PREFIX
from app.models import LalafoAutoReplyMeta, RequestedCardPromotion, ApartmentInventoryQueue
from app.payments.repository import ApartmentRepository
from app.promotions import external_repeat_receipt_key
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from app.telegram.formatting import format_public_apartment
from app.telegram.keyboards import apartment_keyboard
from scripts.managed_requested_repeats import enroll
from scripts.scrape_publish import apartment_to_ad

SUPPORTED_MANAGED_PROFILE_IDS = {18194628, 18209276}


async def recover_confirmed_card(bot, publisher, apartment_id, ad, message_id,
                                 *, append_remaining_photos=True, delete_photo_message_ids=()):
    """Restore photos to a publicly confirmed timed-out card without a second caption."""
    urls = list(dict.fromkeys(ad.photo_urls))
    if not urls or message_id <= 0:
        raise ValueError('Confirmed card and source photos are required')
    message = await publisher._retry(
        bot.edit_message_media, chat_id=publisher.chat_id, message_id=message_id,
        media=InputMediaPhoto(media=urls[0], caption=format_public_apartment(
            ad, bot_username=publisher.bot_username, wanted_deep_link=True), parse_mode='HTML'),
        reply_markup=apartment_keyboard(apartment_id, signer=publisher.signer,
            bot_username=publisher.bot_username, support_url=publisher.support_url,
            include_duplicate=publisher.include_duplicate))
    if delete_photo_message_ids:
        if message_id in delete_photo_message_ids:
            raise ValueError('Cannot delete the confirmed card')
        await publisher._retry(bot.delete_messages, chat_id=publisher.chat_id,
                               message_ids=list(delete_photo_message_ids))
    if append_remaining_photos and len(urls) > 1:
        photos = await publisher._retry(bot.send_media_group, chat_id=publisher.chat_id,
            media=[InputMediaPhoto(media=url) for url in urls[1:10]],
            reply_parameters=ReplyParameters(message_id=message_id))
        if not publisher._photos_acknowledged(photos):
            raise RuntimeError('Restored photos were not acknowledged')
    return message


async def handoff_to_periodic_schedule(sessions, resolved, manifest):
    """The explicit two-hour batch owns these repeats, avoiding queue duplicates."""
    if manifest.get('repeat_every_hours') != 2 or manifest.get('externally_scheduled') is not True:
        raise ValueError('Expected an explicitly scheduled two-hour batch')
    repo = ApartmentRepository(sessions)
    for row, apartment, _ in resolved:
        await repo.save_managed_lalafo_source(
            managed_lalafo_ad_id=row['ad_id'], managed_lalafo_ad_url=row['url'],
            apartment=apartment)
    owners = {row['profile_id'] for row, _, _ in resolved}
    async with sessions.begin() as session:
        markers = await session.scalars(select(LalafoAutoReplyMeta).where(
            LalafoAutoReplyMeta.key.startswith(PREFIX)))
        ids = {apartment.id for _, apartment, _ in resolved}
        for marker in markers:
            value = json.loads(marker.value)
            if value.get('profile_id') in owners:
                value.update(enabled=False, externally_scheduled=True, repeat_every_hours=2)
                marker.value = json.dumps(value)
                ids.add(int(marker.key[len(PREFIX):]))
        await session.execute(update(RequestedCardPromotion).where(
            RequestedCardPromotion.apartment_id.in_(ids)).values(enabled=False))
        await session.execute(update(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.apartment_id.in_(ids),
            ApartmentInventoryQueue.status == 'queued').values(
                status='skipped', last_error='external_two_hour_schedule'))


async def run(manifest_path):
    if os.environ.get('CONFIRM_OWNED_PUBLIC_SEND') != 'true':
        raise ValueError('Explicit public-send confirmation required')
    manifest = json.loads(Path(manifest_path).read_text())
    now = datetime.now(timezone.utc)
    checked = datetime.fromisoformat(manifest['verified_at'])
    if not timedelta(0) <= now - checked <= timedelta(minutes=45):
        raise ValueError('Refresh the owner-visible snapshots before sending')
    cards = manifest['cards']
    external = manifest.get('externally_scheduled') is True and manifest.get('repeat_every_hours') == 2
    if not 1 <= len(cards) <= (30 if external else 10) or len({r['ad_id'] for r in cards}) != len(cards):
        raise ValueError('Expected unique explicitly requested owned ads')
    if manifest.get('sealed_contacts'):
        from tools.lalafo_campaign_session import load_session
        private_contacts = await load_session(manifest['sealed_contacts'],
                                              key_name=manifest['contact_key_name'])
    else:
        private_contacts = []
    supplied = [LalafoAd.model_validate(row) for row in private_contacts]
    supplied = {ad.lalafo_id: ad for ad in supplied}
    settings = get_settings()
    if settings.telegram_group_id != -1004389602150:
        raise ValueError('Unexpected Telegram destination')
    engine, sessions = create_engine_and_session(settings.database_url)
    repo = ApartmentRepository(sessions)
    bot = Bot(settings.require_bot_token())
    publisher = TelegramPublisher(bot, chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url,
        request_timeout_seconds=30)
    try:
        resolved = []
        # Resolve the entire batch before any send. Never manufacture a phone,
        # substitute the operator's phone, or turn the buttons into chat links.
        for row in cards:
            snapshot = row['snapshot']
            # The operator explicitly uses owner-visible payment-waiting cards
            # in Telegram while deciding which Lalafo promotion to fund.  They
            # are safe here because the snapshot comes from the authenticated
            # owner account and the verified original supplies the contact.
            if (row['profile_id'] not in SUPPORTED_MANAGED_PROFILE_IDS
                    or snapshot['user_id'] != row['profile_id']
                    or snapshot['id'] != row['ad_id']
                    or snapshot['status_id'] not in (2, 11)
                    or snapshot['rooms'] not in ('1', 'studio')
                    or snapshot['price'] <= 0):
                raise ValueError('Expected a currently active owned snapshot')
            if row.get('source_id'):
                apartment = await repo.get_by_lalafo(row['source_id'])
            else:
                apartment = await repo.get_by_telegram_message(
                    chat_id=settings.telegram_group_id, message_id=row['telegram_message_id'])
            if row.get('source_id') in supplied:
                original = supplied[row['source_id']]
                if (not original.phone or original.phone_source_version != PHONE_SOURCE_VERSION
                        or original.source_url != row.get('source_url')
                        or not original.source_url.startswith('https://lalafo.kg/')
                        or original.rooms != snapshot['rooms']
                        or original.price != snapshot['price']):
                    raise ValueError('Verified source contact does not match this owned card')
                apartment = await repo.upsert_discovered(original, discovery_priority=True)
            reviewed_chat_card = False
            if apartment is not None and not apartment.phone and apartment.telegram_message_id:
                # Preserve the two previously reviewed chat-only cards and
                # their existing buttons; this batch must not invent a phone.
                async with sessions() as session:
                    prior = await session.get(LalafoAutoReplyMeta, PREFIX + str(apartment.id))
                    binding = json.loads(prior.value) if prior else {}
                reviewed_chat_card = (binding.get('profile_id') == row['profile_id']
                                      and binding.get('ad_id') == row['ad_id'])
            if (apartment is None or (not reviewed_chat_card and (
                    not apartment.phone or apartment.phone_source_version != PHONE_SOURCE_VERSION))):
                raise ValueError(f"Verified original contact missing for managed_id={row['ad_id']}")
            ad = apartment_to_ad(apartment).model_copy(update={
                'price': snapshot['price'], 'rooms': snapshot['rooms'],
                'district': snapshot['district'], 'deposit': snapshot.get('deposit'),
                'photo_urls': snapshot['photo_urls'],
                'source_description': snapshot.get('source_description', ''),
            })
            resolved.append((row, apartment, ad))
        if len({a.id for _, a, _ in resolved}) != len(resolved):
            raise ValueError('Multiple owned ads resolve to the same original apartment')
        for row, apartment, ad in resolved:
            await repo.upsert_discovered(ad, discovery_priority=True)
        for row, apartment, ad in resolved:
            recover_id = None
            cleanup_ids = None
            cleanup = row.get('replace_verified_broken_publication')
            if cleanup:
                old_id = cleanup['card_message_id']
                cleanup_ids = cleanup['message_ids']
                if (not isinstance(old_id, int) or old_id <= 0
                        or not isinstance(cleanup_ids, list) or not 1 <= len(cleanup_ids) <= 100
                        or len(set(cleanup_ids)) != len(cleanup_ids) or old_id not in cleanup_ids
                        or any(not isinstance(i, int) or i <= 0 for i in cleanup_ids)):
                    raise ValueError('Expected exact verified broken publication message IDs')
            key = f"owned_send:{manifest['batch']}:{row['ad_id']}"
            if len(key) > 64:
                raise ValueError('Batch key too long')
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                if marker:
                    if marker.value.startswith('published:'):
                        if cleanup and marker.value == f'published:{old_id}':
                            if apartment.telegram_message_id != old_id:
                                raise ValueError('Broken publication receipt mismatch')
                            marker.value = f'cleanup_for_republish:{old_id}'
                        elif row.get('repair_existing_media') is True:
                            recover_id = int(marker.value.split(':', 1)[1])
                            if recover_id != row.get('publicly_confirmed_message_id'):
                                raise ValueError('Confirmed repair message mismatch')
                            marker.value = 'restoring_confirmed_photos'
                        else:
                            print(f"OWNED_CARD_ALREADY_PUBLISHED managed_id={row['ad_id']} {marker.value}", flush=True)
                            continue
                    elif cleanup and marker.value == f'cleanup_for_republish:{old_id}':
                        pass  # Deleting the same explicitly verified artifacts is idempotent.
                    elif marker.value == 'sending' and row.get('confirmed_not_sent') is True:
                        # Explicitly reconciled recovery error: no caption was sent for this row.
                        pass
                    else:
                        recover_id = row.get('publicly_confirmed_message_id')
                        if not recover_id or marker.value != 'sending':
                            raise RuntimeError('Ambiguous send: reconcile Telegram before retrying')
                        marker.value = 'restoring_confirmed_photos'
                else:
                    if cleanup:
                        raise ValueError('Cannot replace a publication without its existing batch marker')
                    session.add(LalafoAutoReplyMeta(key=key, value='sending'))
            if cleanup_ids:
                await publisher._retry(bot.delete_messages, chat_id=settings.telegram_group_id,
                                       message_ids=cleanup_ids)
                async with sessions.begin() as session:
                    marker = await session.get(LalafoAutoReplyMeta, key)
                    marker.value = 'sending'
            if recover_id:
                message = await recover_confirmed_card(bot, publisher, apartment.id, ad, int(recover_id),
                    append_remaining_photos=row.get('append_recovery_gallery', True),
                    delete_photo_message_ids=row.get('delete_verified_wrong_photo_ids', ()))
            else:
                message = await publisher.publish(apartment.id, ad)
            await repo.mark_published(apartment.id, chat_id=settings.telegram_group_id,
                message_id=message.message_id, acknowledged_at=message.date,
                promote_requested=True)
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                marker.value = f'published:{message.message_id}'
                if external:
                    receipt_key = external_repeat_receipt_key(settings.telegram_group_id, message.message_id)
                    if await session.get(LalafoAutoReplyMeta, receipt_key) is None:
                        session.add(LalafoAutoReplyMeta(key=receipt_key, value='2h'))
            print(f"OWNED_CARD_PUBLISHED managed_id={row['ad_id']} source_id={ad.lalafo_id} "
                  f"message_id={message.message_id} photos={len(ad.photo_urls)}", flush=True)
        # First-time originals have no Telegram receipt until the initial send.
        # Enroll only after every card has an acknowledged publication.
        if external:
            await handoff_to_periodic_schedule(sessions, resolved, manifest)
        else:
            await enroll(sessions, manifest, now=now)
        print(f"OWNED_BATCH_COMPLETED={len(resolved)} repeat_every_hours={manifest.get('repeat_every_hours', 3)}", flush=True)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    asyncio.run(run(parser.parse_args().manifest))

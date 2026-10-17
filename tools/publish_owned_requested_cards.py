"""Send an explicit owned batch with verified stored contacts and 3h repeats."""
import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import PHONE_SOURCE_VERSION, LalafoAd
from app.managed_cards import PREFIX
from app.models import LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from scripts.managed_requested_repeats import enroll
from scripts.scrape_publish import apartment_to_ad


async def run(manifest_path):
    if os.environ.get('CONFIRM_OWNED_PUBLIC_SEND') != 'true':
        raise ValueError('Explicit public-send confirmation required')
    manifest = json.loads(Path(manifest_path).read_text())
    now = datetime.now(timezone.utc)
    checked = datetime.fromisoformat(manifest['verified_at'])
    if not timedelta(0) <= now - checked <= timedelta(minutes=45):
        raise ValueError('Refresh the owner-visible snapshots before sending')
    cards = manifest['cards']
    if not 1 <= len(cards) <= 10 or len({r['ad_id'] for r in cards}) != len(cards):
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
        bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url)
    try:
        resolved = []
        # Resolve the entire batch before any send. Never manufacture a phone,
        # substitute the operator's phone, or turn the buttons into chat links.
        for row in cards:
            snapshot = row['snapshot']
            if (row['profile_id'] != 18194628 or snapshot['user_id'] != 18194628
                    or snapshot['id'] != row['ad_id'] or snapshot['status_id'] != 2
                    or snapshot['rooms'] not in ('1', 'studio')
                    or snapshot['price'] <= 0 or not snapshot['photo_urls']):
                raise ValueError('Expected a currently active owned snapshot')
            if row.get('source_id'):
                apartment = await repo.get_by_lalafo(row['source_id'])
            else:
                apartment = await repo.get_by_telegram_message(
                    chat_id=settings.telegram_group_id, message_id=row['telegram_message_id'])
            if row.get('source_id') in supplied and (apartment is None or not apartment.phone):
                original = supplied[row['source_id']]
                if (not original.phone or original.phone_source_version != PHONE_SOURCE_VERSION
                        or original.source_url != row.get('source_url')
                        or original.photo_urls != row.get('original_photo_urls')
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
            key = f"owned_send:{manifest['batch']}:{row['ad_id']}"
            if len(key) > 64:
                raise ValueError('Batch key too long')
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                if marker:
                    if marker.value.startswith('published:'):
                        print(f"OWNED_CARD_ALREADY_PUBLISHED managed_id={row['ad_id']} {marker.value}", flush=True)
                        continue
                    raise RuntimeError('Ambiguous send: reconcile Telegram before retrying')
                session.add(LalafoAutoReplyMeta(key=key, value='sending'))
            message = await publisher.publish(apartment.id, ad)
            await repo.mark_published(apartment.id, chat_id=settings.telegram_group_id,
                message_id=message.message_id, acknowledged_at=message.date,
                promote_requested=True)
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                marker.value = f'published:{message.message_id}'
            print(f"OWNED_CARD_PUBLISHED managed_id={row['ad_id']} source_id={ad.lalafo_id} "
                  f"message_id={message.message_id} photos={len(ad.photo_urls)}", flush=True)
        # First-time originals have no Telegram receipt until the initial send.
        # Enroll only after every card has an acknowledged publication.
        await enroll(sessions, manifest, now=now)
        print(f'OWNED_BATCH_COMPLETED={len(resolved)} repeat_every_hours=3', flush=True)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    asyncio.run(run(parser.parse_args().manifest))

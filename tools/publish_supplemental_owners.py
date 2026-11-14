"""One explicitly requested batch; encrypted contacts and permanent send reservations."""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.exclusions import is_excluded_contact
from app.lalafo.models import LalafoAd, PHONE_SOURCE_VERSION
from app.models import LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from tools.lalafo_campaign_session import load_session




async def main(path):
    envelope = json.loads(Path(path).read_text())
    data = await load_session(envelope['sealed_card'], key_name=envelope['key_name'])
    checked = datetime.fromisoformat(data['verified_at'])
    if not timedelta(0) <= datetime.now(timezone.utc) - checked <= timedelta(hours=2):
        raise ValueError('Reviewed sources are stale')
    ads = [LalafoAd.model_validate(row) for row in data['ads']]
    if not 1 <= len(ads) <= 10 or len({ad.lalafo_id for ad in ads}) != len(ads):
        raise ValueError('Expected one to ten distinct reviewed owner offers')
    evidence = data['evidence']
    for ad in ads:
        proof = evidence.get(str(ad.lalafo_id), {})
        if not proof.get('owner_claim') or not proof.get('central_location') or not proof.get('source_url') == ad.source_url:
            raise ValueError('Missing exact-source owner and location evidence')
    for ad in ads:
        if (not ad.phone or ad.phone_source_version != PHONE_SOURCE_VERSION
                or ad.rooms not in {'studio', '1', '2'} or not ad.no_subletting
                or not ad.owner_listing or ad.seller_type != 'owner'
                or not ad.photo_urls or not ad.source_url.startswith(('https://lalafo.kg/', 'https://t.me/'))
                or not 10000 <= ad.price <= {'studio':30000,'1':35000,'2':45000}[ad.rooms]
                or is_excluded_contact(ad)):
            raise ValueError('Unverified source or excluded contact')
    settings = get_settings()
    if settings.telegram_group_id != -1004389602150:
        raise ValueError('Unexpected destination')
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    repo = ApartmentRepository(sessions)
    publisher = TelegramPublisher(bot, chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url,
        max_photos=5, request_timeout_seconds=45)
    try:
        duplicates = await repo.duplicate_candidate_ids(ads)
        for ad in ads:
            key = f'supplemental_owner_once:{ad.lalafo_id}'
            async with sessions() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                prior = marker.value if marker else None
            if prior:
                if not prior.startswith('published:'):
                    raise RuntimeError('Uncertain prior send; reconcile before replay')
                print(f'RECEIPT source_id={ad.lalafo_id} {prior}', flush=True)
                continue
            existing = await repo.get_by_lalafo(ad.lalafo_id)
            if ad.lalafo_id in duplicates:
                print(f'ALREADY_PUBLISHED source_id={ad.lalafo_id} message_id={existing.telegram_message_id if existing else None}', flush=True)
                continue
            apartment = await repo.upsert_discovered(ad, discovery_priority=True)
            async with sessions.begin() as session:
                if await session.get(LalafoAutoReplyMeta, key) is not None:
                    raise RuntimeError('Concurrent reservation')
                session.add(LalafoAutoReplyMeta(key=key, value='sending'))
            # Never retry an uncertain send or swap the contact to an agency number.
            message = await publisher.publish(apartment.id, ad)
            await repo.mark_published(apartment.id, chat_id=settings.telegram_group_id,
                message_id=message.message_id, acknowledged_at=message.date)
            async with sessions.begin() as session:
                marker = await session.get(LalafoAutoReplyMeta, key)
                marker.value = f'published:{message.message_id}'
            print(f'PUBLISHED source_id={ad.lalafo_id} message_id={message.message_id} url=https://t.me/bishkekarendakg/{message.message_id}', flush=True)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--envelope', required=True)
    asyncio.run(main(parser.parse_args().envelope))

"""Deliver explicitly reviewed chat-only cards without promising a hidden phone."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import LalafoAd
from app.models import LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.telegram.publisher import TelegramPublisher
from app.security import TokenSigner


def reviewed_card(row):
    ad = LalafoAd.model_validate(row['ad'])
    target = urlsplit(row['chat_url'])
    source = urlsplit(ad.source_url)
    if (ad.phone or ad.rooms not in {'1', 'studio'} or not ad.photo_urls
            or source.scheme != 'https' or source.hostname != 'lalafo.kg'
            or target.scheme != 'https' or target.hostname != 'lalafo.kg'
            or target.query or target.fragment
            or not target.path.endswith(f"-id-{int(row['managed_ad_id'])}")
            or row['owner_profile_id'] != 18194628):
        raise ValueError('Expected a reviewed chat-only card and exact managed ad URL')
    return ad


async def publish_card(row, *, sessions, bot, settings):
    ad = reviewed_card(row)
    key = f"requested_chat_card:{ad.lalafo_id}:{row['managed_ad_id']}"
    async with sessions.begin() as session:
        marker = await session.get(LalafoAutoReplyMeta, key)
        if marker:
            if marker.value.startswith('published:'):
                print(f'CHAT_CARD_ALREADY_PUBLISHED source_id={ad.lalafo_id} {marker.value}')
                return int(marker.value.split(':')[1])
            raise RuntimeError('Ambiguous earlier send: reconcile Telegram before retrying')
        session.add(LalafoAutoReplyMeta(key=key, value='sending'))
    repo = ApartmentRepository(sessions)
    existing = await repo.get_by_lalafo(ad.lalafo_id)
    if existing and existing.phone:
        raise RuntimeError('Preserve verified contact; use the normal publisher')
    apartment = await repo.upsert_discovered(ad, discovery_priority=True)
    # Persist sending before Telegram. Never retry an ambiguous send blindly.
    # The shared publisher requires an acknowledged photo before the text card.
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=TokenSigner(settings.require_callback_secret()),
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,
    )
    message = await publisher.publish(apartment.id, ad)
    await repo.mark_published(apartment.id, chat_id=settings.telegram_group_id,
                              message_id=message.message_id, acknowledged_at=message.date)
    async with sessions.begin() as session:
        marker = await session.get(LalafoAutoReplyMeta, key)
        marker.value = f'published:{message.message_id}'
    print(f'CHAT_CARD_PUBLISHED source_id={ad.lalafo_id} message_id={message.message_id} photos={len(ad.photo_urls)}')
    return message.message_id


async def main():
    if os.environ.get('CONFIRM_CHAT_CARD_PUBLIC_SEND') != 'true':
        raise RuntimeError('Explicit public-send confirmation required')
    rows = json.loads(Path(os.environ['CHAT_CARD_MANIFEST']).read_text())
    if not 1 <= len(rows) <= 3:
        raise ValueError('Expected one to three explicitly requested cards')
    for row in rows:
        reviewed_card(row)
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    try:
        for row in rows:
            await publish_card(row, sessions=sessions, bot=bot, settings=settings)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

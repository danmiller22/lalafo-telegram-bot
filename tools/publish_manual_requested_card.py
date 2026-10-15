"""Publish one authorized owner card with an encrypted private contact."""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select, func

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import LalafoAd
from app.models import DailyFeaturedPublication, LalafoAutoReplyMeta
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from tools.lalafo_campaign_session import load_session


async def publish_card(data, sessions, bot, settings):
    ad = LalafoAd.model_validate(data['ad'])
    if ad.lalafo_id >= 0 or ad.source_url != f'manual://telegram/{abs(ad.lalafo_id)}':
        raise ValueError('Expected a uniquely identified manual card')
    if ad.rooms not in {'1', 'studio'} or not ad.phone or not ad.photo_urls:
        raise ValueError('Incomplete manual card')
    apartments = ApartmentRepository(sessions)
    apartment = await apartments.upsert_discovered(ad, discovery_priority=True)
    marker = f'manual_requested_send:{ad.lalafo_id}'
    message_id = apartment.telegram_message_id
    if not message_id:
        async with sessions.begin() as session:
            if await session.get(LalafoAutoReplyMeta, marker) is not None:
                raise RuntimeError('An earlier manual send needs receipt reconciliation before retry')
            session.add(LalafoAutoReplyMeta(key=marker,value='sending'))
        publisher = TelegramPublisher(bot,chat_id=settings.telegram_group_id,
            signer=TokenSigner(settings.require_callback_secret()),
            bot_username=settings.telegram_bot_username,support_url=settings.support_bot_url)
        sent = await publisher.publish(apartment.id,ad)
        message_id = sent.message_id
        await apartments.mark_published(apartment.id,chat_id=settings.telegram_group_id,
            message_id=message_id,promote_requested=True,acknowledged_at=sent.date)
    day = datetime.now(ZoneInfo('Asia/Bishkek')).date()
    async with sessions.begin() as session:
        existing = await session.scalar(select(DailyFeaturedPublication).where(
            DailyFeaturedPublication.managed_lalafo_ad_id == data['managed_ad_id']))
        if existing is not None:
            if existing.source_apartment_id != apartment.id:
                raise RuntimeError('Managed advertisement belongs to another source')
        else:
            slot = int(await session.scalar(select(func.max(DailyFeaturedPublication.slot)).where(
                DailyFeaturedPublication.business_date == day)) or 0)+1
            session.add(DailyFeaturedPublication(business_date=day,slot=slot,
                source_apartment_id=apartment.id,source_lalafo_id=ad.lalafo_id,
                managed_lalafo_ad_id=data['managed_ad_id'],managed_lalafo_ad_url=data['managed_ad_url'],
                lalafo_publication_status=data['managed_publication_status'],
                managed_lalafo_uploaded_photos=len(ad.photo_urls),
                telegram_message_id=message_id,telegram_chat_id=settings.telegram_group_id))
        row = await session.get(LalafoAutoReplyMeta,marker)
        if row is None:
            session.add(LalafoAutoReplyMeta(key=marker,value=f'published:{message_id}'))
        else:
            row.value = f'published:{message_id}'
    print(f'MANUAL_PUBLISHED message_id={message_id} photos={len(ad.photo_urls)} price={ad.price}')
    return message_id


async def main(path):
    envelope=json.loads(Path(path).read_text())
    data=await load_session(envelope['sealed_card'],key_name=envelope['key_name'])
    settings=get_settings()
    engine,sessions=create_engine_and_session(settings.database_url)
    bot=Bot(settings.require_bot_token())
    try:
        await publish_card(data,sessions,bot,settings)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--envelope',required=True)
    asyncio.run(main(parser.parse_args().envelope))

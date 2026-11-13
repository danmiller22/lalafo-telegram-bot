"""Refresh recorded public cards with the current caption, without new sends."""
import asyncio
import json
from types import SimpleNamespace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, RequestedCardPublication
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment, is_confirmed_owner
from app.telegram.keyboards import apartment_keyboard


async def main():
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    stats = dict(updated=0, unchanged=0, missing=0, errors=0)
    try:
        async with sessions() as session:
            apartments = list((await session.scalars(select(Apartment).where(
                Apartment.telegram_chat_id == settings.telegram_group_id,
                Apartment.telegram_message_id.is_not(None),
            ).order_by(Apartment.published_at.desc()))).all())
            receipts = list((await session.execute(select(Apartment, RequestedCardPublication)
                .join(RequestedCardPublication, Apartment.id == RequestedCardPublication.apartment_id)
                .where(RequestedCardPublication.telegram_chat_id == settings.telegram_group_id)
                .order_by(RequestedCardPublication.published_at.desc()))).all())
        targets = {a.telegram_message_id: a for a in apartments}
        for apartment, receipt in receipts:
            # Historical repeats retain the price and area saved at that send.
            snapshot = SimpleNamespace(**{column.name: getattr(apartment, column.name)
                                          for column in Apartment.__table__.columns})
            snapshot.price = receipt.price
            snapshot.district = receipt.district
            targets.setdefault(receipt.telegram_message_id, snapshot)
        signer = TokenSigner(settings.require_callback_secret())
        print('CAPTION_REFRESH_TARGETS ' + str(len(targets)), flush=True)
        for message_id, apartment in targets.items():
            archived = not apartment.active or apartment.availability_status == 'unavailable'
            keyboard = (InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='🏠 Смотреть квартиры', url=f'https://t.me/{settings.telegram_bot_username.lstrip("@")}')
            ]]) if archived else apartment_keyboard(
                apartment.id, phone=apartment.phone, confirmed_owner=is_confirmed_owner(apartment),
                signer=signer, bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url,
            ))
            for attempt in range(3):
                try:
                    await bot.edit_message_text(
                        chat_id=settings.telegram_group_id, message_id=message_id,
                        text=format_public_apartment(apartment, bot_username=settings.telegram_bot_username,
                                                     wanted_deep_link=True, archived=archived),
                        parse_mode='HTML', reply_markup=keyboard,
                    )
                    stats['updated'] += 1
                    break
                except TelegramRetryAfter as error:
                    if attempt == 2:
                        stats['errors'] += 1
                    else:
                        await asyncio.sleep(error.retry_after + 1)
                except TelegramBadRequest as error:
                    detail = str(error).lower()
                    if 'message is not modified' in detail:
                        stats['unchanged'] += 1
                    elif 'message to edit not found' in detail or "message can't be edited" in detail:
                        stats['missing'] += 1
                    else:
                        stats['errors'] += 1
                    break
            await asyncio.sleep(0.1)
        print('CAPTION_REFRESH_RESULT ' + json.dumps(stats), flush=True)
        if stats['errors']:
            raise RuntimeError('Some recorded card edits failed; inspect aggregate result')
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

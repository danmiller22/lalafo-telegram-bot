"""Correct three verified owned cards; preserve photos and standard buttons."""
import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, RequestedCardPublication
from app.security import TokenSigner
from app.telegram.formatting import is_confirmed_owner, format_public_apartment
from app.telegram.keyboards import apartment_keyboard

CORRECTIONS = [(46983, 'Вефа ТЦ', '1'), (47024, 'Филармония', '1'),
               (46988, 'Восток-5', 'studio')]

async def main():
    settings = get_settings()
    assert settings.telegram_group_id == -1004389602150
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    try:
        for message_id, district, rooms in CORRECTIONS:
            async with sessions.begin() as session:
                aid = await session.scalar(select(RequestedCardPublication.apartment_id).where(
                    RequestedCardPublication.telegram_chat_id == settings.telegram_group_id,
                    RequestedCardPublication.telegram_message_id == message_id))
                assert aid is not None
                apartment = await session.get(Apartment, aid)
                apartment.district, apartment.rooms = district, rooms
                try:
                    await bot.edit_message_text(chat_id=settings.telegram_group_id,
                        message_id=message_id,
                        text=format_public_apartment(apartment,
                            bot_username=settings.telegram_bot_username, wanted_deep_link=True),
                        parse_mode='HTML', reply_markup=apartment_keyboard(aid,
                            phone=apartment.phone, confirmed_owner=is_confirmed_owner(apartment), signer=TokenSigner(settings.require_callback_secret()),
                            bot_username=settings.telegram_bot_username,
                            support_url=settings.support_bot_url))
                except TelegramBadRequest as exc:
                    if 'message is not modified' not in str(exc).lower():
                        raise
                print(f'OWNED_CARD_LABEL_CORRECTED message_id={message_id}', flush=True)
    finally:
        await bot.session.close()
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())

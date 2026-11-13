"""Edit only the operator's acknowledged Vostok-5 card, without a new send."""
import argparse
import asyncio
import json
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.models import LalafoAd
from app.models import Apartment
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment, is_confirmed_owner
from app.telegram.keyboards import apartment_keyboard
from tools.lalafo_campaign_session import load_session


async def main(path):
    envelope = json.loads(Path(path).read_text())
    data = await load_session(envelope['sealed_card'], key_name=envelope['key_name'])
    assert data['managed_ad_id'] == 113786820
    ad = LalafoAd.model_validate(data['ad']).model_copy(update={'deposit': None})
    settings = get_settings()
    assert settings.telegram_group_id == -1004389602150
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    try:
        apartment = await ApartmentRepository(sessions).get_by_lalafo(ad.lalafo_id)
        assert apartment and apartment.telegram_message_id == 61601
        assert apartment.telegram_chat_id == settings.telegram_group_id
        assert apartment.phone == ad.phone
        text = format_public_apartment(ad, bot_username=settings.telegram_bot_username, wanted_deep_link=True)
        assert 'депозит' not in text.casefold()
        keyboard = apartment_keyboard(apartment.id, phone=ad.phone,
            confirmed_owner=is_confirmed_owner(ad), signer=TokenSigner(settings.require_callback_secret()),
            bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url)
        try:
            await bot.edit_message_text(chat_id=settings.telegram_group_id, message_id=61601,
                text=text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest as error:
            if 'message is not modified' not in str(error).lower():
                raise
        async with sessions.begin() as session:
            await session.execute(update(Apartment).where(Apartment.id == apartment.id).values(deposit=None))
        print('CARD_EDIT_VERIFIED message_id=61601 deposit_omitted=true new_messages=0', flush=True)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--envelope', required=True)
    asyncio.run(main(parser.parse_args().envelope))

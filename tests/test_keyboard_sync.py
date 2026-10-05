from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.config import Settings
from app.models import Apartment
from app.security import TokenSigner
from app.telegram.keyboard_sync import sync_published_keyboards
from app.telegram.keyboards import APARTMENT_KEYBOARD_VERSION
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_refresh_existing_card_buttons_without_republishing(repositories, monkeypatch):
    apartments, _, sessions = repositories
    old = await apartments.upsert_discovered(make_ad(lalafo_id=18801))
    fresh = await apartments.upsert_discovered(make_ad(lalafo_id=18802))
    await apartments.mark_published(old.id, chat_id=-100123, message_id=101)
    await apartments.mark_published(fresh.id, chat_id=-100123, message_id=102)
    async with sessions.begin() as session:
        await session.execute(update(Apartment).where(Apartment.id == old.id).values(keyboard_version=15))
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())
    runtime = SimpleNamespace(bot=bot, workflow_data={"apartments": apartments,
        "settings": Settings(telegram_bot_username="testbot"), "signer": TokenSigner("sync-secret-long-enough")})
    monkeypatch.setattr("app.telegram.keyboard_sync.asyncio.sleep", AsyncMock())
    await sync_published_keyboards(runtime)
    bot.edit_message_reply_markup.assert_awaited_once()
    call = bot.edit_message_reply_markup.await_args.kwargs
    assert call["chat_id"] == -100123 and call["message_id"] == 101
    assert all("Подать заявку" not in b.text for row in call["reply_markup"].inline_keyboard for b in row)
    assert (await apartments.get(old.id)).keyboard_version == APARTMENT_KEYBOARD_VERSION
    await sync_published_keyboards(runtime)
    bot.edit_message_reply_markup.assert_awaited_once()

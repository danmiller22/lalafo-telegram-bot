from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from scripts.managed_requested_repeats import enroll
from scripts import restore_managed_card_buttons as repair
from tests.helpers import make_ad
from tests.test_managed_requested_repeats import BINDING, NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('all_cards', [False, True])
async def test_repair_restores_both_historical_and_latest_card_buttons(repositories, monkeypatch, all_cards):
    class Clock(repair.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(repair, 'datetime', Clock)
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(phone='+996555123456'))
    await apartments.mark_published(apartment.id, chat_id=-1004389602150,
                                    message_id=100, acknowledged_at=NOW)
    await enroll(sessions, {'batch': 'restore-buttons',
        'cards': [{**BINDING, 'source_id': 12345}]}, now=NOW)
    await apartments.mark_published(apartment.id, chat_id=-1004389602150,
                                    message_id=101, acknowledged_at=NOW)
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(),
                          session=SimpleNamespace(close=AsyncMock()))
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(repair, 'create_engine_and_session', lambda _: (engine, sessions))
    monkeypatch.setattr(repair, 'Bot', lambda **_: bot)
    monkeypatch.setattr(repair, 'get_settings', lambda: Settings(_env_file=None,
        telegram_bot_token='123:fake', callback_secret='test-secret-long-enough',
        telegram_group_id=-1004389602150))
    monkeypatch.delenv('FOLLOW_PUBLICATION_RUN', raising=False)
    monkeypatch.setenv('ALL_PUBLIC_CARDS', str(all_cards).lower())
    if all_cards:
        ordinary = await apartments.upsert_discovered(make_ad(lalafo_id=54321, phone='+996555999111'))
        await apartments.mark_published(ordinary.id, chat_id=-1004389602150,
                                        message_id=102, acknowledged_at=NOW)
    assert await repair.run() == 0
    calls = bot.edit_message_reply_markup.await_args_list
    assert {call.kwargs['message_id'] for call in calls} == ({100, 101, 102} if all_cards else {100, 101})
    for call in calls:
        rows = call.kwargs['reply_markup'].inline_keyboard
        expected = '+996555999***' if call.kwargs['message_id'] == 102 else '+996555123***'
        assert rows[0][0].text == expected and '/access?startapp=' in rows[0][0].url
        assert rows[1][0].text == '🔄 Проверить актуальность'
        assert rows[1][0].callback_data.startswith('availability:')

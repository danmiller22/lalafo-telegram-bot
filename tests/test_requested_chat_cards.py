from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.helpers import make_ad
from tools.publish_requested_chat_cards import publish_card, reviewed_card


def card():
    return {
        'ad': make_ad(phone='', photo_urls=['https://img/room', 'https://img/kitchen'],
                      source_description='Без мебели. Депозит 10000 сом.').model_dump(),
        'chat_url': 'https://lalafo.kg/bishkek/ads/example-id-117338213',
        'managed_ad_id': 117338213, 'owner_profile_id': 18194628,
    }


@pytest.mark.asyncio
async def test_reviewed_delivery_keeps_standard_buttons_and_is_idempotent(repositories):
    _, _, sessions = repositories
    bot = SimpleNamespace(send_media_group=AsyncMock(), send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=45899, date=datetime.now(timezone.utc))))
    settings = SimpleNamespace(telegram_group_id=-100123, telegram_bot_username='arenda312bot',
                               support_bot_url='https://t.me/arenda312bot?start=support',
                               require_callback_secret=lambda: 'test-secret-long-enough')
    await publish_card(card(), sessions=sessions, bot=bot, settings=settings)
    await publish_card(card(), sessions=sessions, bot=bot, settings=settings)
    bot.send_message.assert_awaited_once()
    bot.send_media_group.assert_awaited_once()
    message = bot.send_message.call_args.kwargs
    assert message['reply_markup'].inline_keyboard[0][0].text == 'Получить номер'
    assert '/access?startapp=' in message['reply_markup'].inline_keyboard[0][0].url
    assert message['reply_markup'].inline_keyboard[1][0].text == '🔄 Проверить актуальность'
    assert 'Депозит 10000' in message['text']


@pytest.mark.asyncio
async def test_ambiguous_delivery_requires_reconciliation_before_retry(repositories):
    _, _, sessions = repositories
    bot = SimpleNamespace(send_media_group=AsyncMock(), send_message=AsyncMock(side_effect=TimeoutError))
    settings = SimpleNamespace(telegram_group_id=-100123, telegram_bot_username='arenda312bot',
                               support_bot_url='https://t.me/arenda312bot?start=support',
                               require_callback_secret=lambda: 'test-secret-long-enough')
    with pytest.raises(TimeoutError):
        await publish_card(card(), sessions=sessions, bot=bot, settings=settings)
    with pytest.raises(RuntimeError, match='reconcile'):
        await publish_card(card(), sessions=sessions, bot=bot, settings=settings)
    bot.send_media_group.assert_awaited_once()


def test_chat_destination_cannot_be_replaced_with_an_external_site():
    row = card()
    row['chat_url'] = 'https://example.com/bishkek/ads/example-id-117338213'
    with pytest.raises(ValueError):
        reviewed_card(row)

import json
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.managed_cards import PREFIX, managed_bindings
from app.models import LalafoAutoReplyMeta, RequestedCardPromotion
from app.promotions import register_requested, promotion_work
from tests.helpers import make_ad
from tools.publish_owned_requested_cards import handoff_to_periodic_schedule


@pytest.mark.asyncio
async def test_two_hour_external_batch_preserves_source_mapping_without_queue_repeats(repositories):
    repo, _, sessions = repositories
    apartment = await repo.upsert_discovered(make_ad())
    now = datetime.now(timezone.utc)
    binding = {'profile_id': 18209276, 'ad_id': 117635496,
               'url': 'https://lalafo.kg/bishkek/ads/example-id-117635496',
               'enabled': True, 'repeat_every_hours': 3, 'repeat_while_active': True}
    async with sessions.begin() as session:
        await register_requested(session, apartment, now=now)
        session.add(LalafoAutoReplyMeta(key=PREFIX+str(apartment.id), value=json.dumps(binding)))
    manifest = {'repeat_every_hours': 2, 'externally_scheduled': True}
    await handoff_to_periodic_schedule(sessions, [(binding, apartment, make_ad())], manifest)
    await handoff_to_periodic_schedule(sessions, [(binding, apartment, make_ad())], manifest)
    async with sessions() as session:
        promotion = await session.get(RequestedCardPromotion, apartment.id)
        assert not promotion.enabled
        assert not await managed_bindings(session)
        due, _ = await promotion_work(session, now=now+timedelta(hours=6),
                                     day_start=now, day_end=now+timedelta(days=1))
        assert due == []
    links = await repo.managed_lalafo_sources()
    assert len(links) == 1
    assert links[0].managed_lalafo_ad_id == binding['ad_id']
    assert links[0].apartment.phone == apartment.phone


@pytest.mark.asyncio
async def test_confirmed_timeout_card_restores_photos_without_duplicate_caption():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.security import TokenSigner
    from app.telegram.publisher import TelegramPublisher
    from tools.publish_owned_requested_cards import recover_confirmed_card
    ad = make_ad()
    ad.photo_urls = ['https://example.test/room.jpg', 'https://example.test/kitchen.jpg', 'https://example.test/bath.jpg']
    existing = SimpleNamespace(message_id=53072)
    bot = SimpleNamespace(edit_message_media=AsyncMock(return_value=existing),
                          send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]),
                          send_message=AsyncMock())
    publisher = TelegramPublisher(bot, chat_id=-1004389602150, signer=TokenSigner('test-secret-for-recovery'),
                                   bot_username='arenda312bot', support_url='https://t.me/support',
                                   request_timeout_seconds=30)
    assert await recover_confirmed_card(bot, publisher, 77, ad, 53072) is existing
    bot.send_message.assert_not_awaited()
    call = bot.edit_message_media.await_args.kwargs
    assert call['message_id'] == 53072 and call['media'].media == ad.photo_urls[0]
    assert call['reply_markup'].inline_keyboard[0][0].text == 'Получить номер'
    album = bot.send_media_group.await_args.kwargs
    assert [m.media for m in album['media']] == ad.photo_urls[1:]
    assert album['reply_parameters'].message_id == 53072

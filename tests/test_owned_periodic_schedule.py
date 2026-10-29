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


@pytest.mark.asyncio
@pytest.mark.parametrize('replace_broken', [False, True])
@pytest.mark.parametrize('invalid_snapshot', [None, 'studio', 'payment_waiting', 'old_profile'])
async def test_recovery_does_not_reuse_message_for_next_apartment(repositories, monkeypatch, tmp_path, replace_broken, invalid_snapshot):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.config import Settings
    from tools import publish_owned_requested_cards as module
    repo, _, sessions = repositories
    now = datetime.now(timezone.utc)
    ads = [make_ad(lalafo_id=710001), make_ad(lalafo_id=710002)]
    originals = [await repo.upsert_discovered(ad) for ad in ads]
    cards = []
    for index, ad in enumerate(ads):
        cards.append({'source_id': ad.lalafo_id, 'profile_id': 18225120, 'ad_id': 720001+index,
                      'url': f'https://lalafo.kg/bishkek/ads/example-id-{720001+index}',
                      'snapshot': {'user_id': 18225120, 'id': 720001+index, 'status_id': 2,
                                   'rooms': '1', 'price': ad.price, 'district': ad.district,
                                   'photo_urls': ad.photo_urls}})
    if replace_broken:
        cards[0]['replace_verified_broken_publication'] = {'card_message_id': 600, 'message_ids': [600, 602, 603]}
        await repo.mark_published(originals[0].id, chat_id=-1004389602150,
                                  message_id=600, acknowledged_at=now)
    else:
        cards[0]['publicly_confirmed_message_id'] = 600
    manifest = {'batch': 'recovery-isolation', 'verified_at': now.isoformat(),
                'repeat_every_hours': 2, 'externally_scheduled': True, 'cards': cards}
    if invalid_snapshot == 'studio':
        cards[0]['snapshot']['rooms'] = 'studio'
    elif invalid_snapshot == 'payment_waiting':
        cards[0]['snapshot']['status_id'] = 11
    elif invalid_snapshot == 'old_profile':
        cards[0]['profile_id'] = 18209276
        cards[0]['snapshot']['user_id'] = 18209276
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    async with sessions.begin() as session:
        session.add(LalafoAutoReplyMeta(key='owned_send:recovery-isolation:720001',
                                       value='published:600' if replace_broken else 'sending'))
    publisher = SimpleNamespace(publish=AsyncMock(side_effect=[SimpleNamespace(message_id=i, date=now) for i in ([604, 601] if replace_broken else [601])]),
                                _retry=AsyncMock())
    recover = AsyncMock(return_value=SimpleNamespace(message_id=600, date=now))
    settings = Settings(_env_file=None, telegram_bot_token='123:test', callback_secret='long-test-secret-value', telegram_group_id=-1004389602150)
    monkeypatch.setenv('CONFIRM_OWNED_PUBLIC_SEND','true')
    monkeypatch.setattr(module,'get_settings',lambda:settings)
    monkeypatch.setattr(module,'create_engine_and_session',lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    bot = SimpleNamespace(delete_messages=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(module,'Bot',lambda _:bot)
    monkeypatch.setattr(module,'TelegramPublisher',lambda *args,**kwargs:publisher)
    monkeypatch.setattr(module,'recover_confirmed_card',recover)
    monkeypatch.setattr(module,'handoff_to_periodic_schedule',AsyncMock())
    if invalid_snapshot:
        with pytest.raises(ValueError, match='active one-room'):
            await module.run(str(path))
        recover.assert_not_awaited()
        publisher.publish.assert_not_awaited()
        return
    await module.run(str(path))
    if replace_broken:
        recover.assert_not_awaited()
        publisher._retry.assert_awaited_once_with(bot.delete_messages, chat_id=-1004389602150, message_ids=[600, 602, 603])
        assert publisher.publish.await_count == 2
        assert publisher.publish.await_args_list[0].args[0] == originals[0].id
        # A rerun of the exact same manifest must neither delete nor republish again.
        await module.run(str(path))
        assert publisher.publish.await_count == 2
        assert publisher._retry.await_count == 1
    else:
        recover.assert_awaited_once()
        assert recover.await_args.args[2] == originals[0].id
        publisher.publish.assert_awaited_once()
    assert publisher.publish.await_args.args[0] == originals[1].id
    assert (await repo.get(originals[0].id)).telegram_message_id == (604 if replace_broken else 600)
    assert (await repo.get(originals[1].id)).telegram_message_id == 601

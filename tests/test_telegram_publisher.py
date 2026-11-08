from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishAmbiguousError, TelegramPublishError, TelegramPublisher


def make_ad() -> LalafoAd:
    return LalafoAd(
        lalafo_id=123,
        source_url="https://lalafo.kg/ad-id-123",
        phone="+996700000000",
        price=25_000,
        currency="KGS",
        rooms="1",
        district="ЦУМ",
        city="Бишкек",
        photo_urls=["https://img.example/1.jpg", "https://img.example/2.jpg"],
        category_id=2044,
        no_subletting=True,
        owner_listing=True,
        seller_type="owner",
    )


@pytest.mark.asyncio
async def test_public_album_uses_fast_direct_telegram_urls() -> None:
    card = SimpleNamespace(message_id=3)
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        ),
        send_message=AsyncMock(return_value=card),
        send_photo=AsyncMock(),
        delete_message=AsyncMock(),
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
        max_photos=10,
    )

    result = await publisher.publish(77, make_ad())

    assert result is card
    card_call = bot.send_message.await_args
    assert card_call.kwargs["parse_mode"] == "HTML"
    assert (
        '<a href="https://t.me/testbot?start=want">@testbot</a>'
        in card_call.kwargs["text"]
    )
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "+996700000***",
        "🛟 Техподдержка",
    ]
    assert "✅ Актуально на момент публикации" in card_call.kwargs["text"]
    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == [
        "https://img.example/1.jpg",
        "https://img.example/2.jpg",
    ]
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_stalled_telegram_send_requires_reconciliation_without_another_send(monkeypatch) -> None:
    import asyncio
    import app.telegram.publisher as publisher_module

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(10)

    bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=stalled),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        send_photo=AsyncMock(side_effect=stalled),
        delete_message=AsyncMock(),
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
    )
    monkeypatch.setattr(publisher_module, 'TELEGRAM_REQUEST_TIMEOUT_SECONDS', 0.001)

    with pytest.raises(TelegramPublishAmbiguousError):
        await publisher.publish(77, make_ad())
    bot.send_media_group.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_caption_response_does_not_duplicate_caption_or_delete_its_album():
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage
    bot = SimpleNamespace(
        send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]),
        send_message=AsyncMock(side_effect=TelegramNetworkError(method=SendMessage(chat_id=-1001,text='card'),message='Response lost')),
        send_photo=AsyncMock(),delete_message=AsyncMock())
    publisher = TelegramPublisher(bot,chat_id=-1001,signer=TokenSigner('s'*32),bot_username='testbot',support_url='https://t.me/support')
    with pytest.raises(TelegramPublishAmbiguousError):
        await publisher.publish(77,make_ad())
    bot.send_message.assert_awaited_once()
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_album_uses_the_first_working_individual_photo() -> None:
    card = SimpleNamespace(message_id=3)
    photo = SimpleNamespace(message_id=2)
    bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=RuntimeError("bad album photo")),
        send_message=AsyncMock(return_value=card),
        send_photo=AsyncMock(side_effect=[RuntimeError("bad first photo"), photo]),
        delete_message=AsyncMock(),
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
    )

    assert await publisher.publish(77, make_ad()) is card
    assert bot.send_photo.await_count == 2
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_telegram_file_ids_form_album_with_working_card_keyboard() -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        send_photo=AsyncMock(),
        delete_message=AsyncMock(),
    )
    ad = make_ad().model_copy(
        update={
            "photo_urls": ["telegram-file-id-1", "telegram-file-id-2"],
            "seller_type": "owner",
            "owner_listing": True,
        }
    )
    signer = TokenSigner("s" * 32)
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=signer,
        bot_username="testbot",
        support_url="https://t.me/support",
    )

    await publisher.publish(77, ad)

    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == ad.photo_urls
    card = bot.send_message.await_args.kwargs
    assert "Автор:" not in card["text"]
    assert "Статус:" not in card["text"]
    keyboard = card["reply_markup"]
    assert keyboard.inline_keyboard[0][0].url.startswith(
        "https://t.me/testbot/access?startapp="
    )
    token = keyboard.inline_keyboard[0][0].url.split("startapp=", 1)[1]
    assert signer.decode_public_start_id(token) == 77


@pytest.mark.asyncio
@pytest.mark.parametrize("seller_type,owner_listing", [("realtor", False), ("realtor", True), ("owner", False)])
async def test_non_owner_author_types_are_rejected(seller_type, owner_listing) -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
    )
    publisher = TelegramPublisher(
        bot, chat_id=-1001, signer=TokenSigner("s" * 32),
        bot_username="testbot", support_url="https://t.me/support",
    )
    ad = make_ad().model_copy(update={"seller_type": seller_type, "owner_listing": owner_listing})

    with pytest.raises(TelegramPublishError, match="realtor|inconsistent"):
        await publisher.publish(77, ad)
    bot.send_media_group.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_album_acknowledgement_never_falls_back_to_text_card() -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(return_value=[]),
        send_photo=AsyncMock(return_value=None),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        delete_message=AsyncMock(),
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
    )

    with pytest.raises(TelegramPublishError, match="photos"):
        await publisher.publish(77, make_ad())
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_file_id_publish_error_never_falls_back_to_text() -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=RuntimeError("Telegram rejected file ID")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        send_photo=AsyncMock(),
        delete_message=AsyncMock(),
    )
    ad = make_ad().model_copy(update={"photo_urls": ["file-1", "file-2"]})
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
    )

    with pytest.raises(TelegramPublishError, match="photos"):
        await publisher.publish(77, ad)

    bot.send_media_group.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_less_apartment_is_never_sent_to_channel() -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(),
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
    )

    with pytest.raises(TelegramPublishError, match="photos"):
        await publisher.publish(77, make_ad().model_copy(update={"photo_urls": []}))

    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_card_caps_photos_to_one_fast_album() -> None:
    card = SimpleNamespace(message_id=30)
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=index) for index in range(1, 6)]
        ),
        send_message=AsyncMock(return_value=card),
        send_photo=AsyncMock(),
        delete_message=AsyncMock(),
    )
    ad = make_ad().model_copy(
        update={
            "photo_urls": [
                f"https://img.example/{index}.jpg" for index in range(1, 13)
            ]
        }
    )
    publisher = TelegramPublisher(
        bot,
        chat_id=-1001,
        signer=TokenSigner("s" * 32),
        bot_username="testbot",
        support_url="https://t.me/support",
        max_photos=10,
    )

    result = await publisher.publish(77, ad)

    assert result is card
    assert bot.send_media_group.await_count == 1
    sent_urls = [
        item.media
        for call in bot.send_media_group.await_args_list
        for item in call.kwargs["media"]
    ]
    assert sent_urls == ad.photo_urls[:5]
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["https://www.salut.kg", "Агентство Салют", "SALUT.KG"])
async def test_salut_source_is_blocked_before_sending_photos(marker):
    from scripts.scrape_publish import is_substandard_structure

    bot = SimpleNamespace(send_photo=AsyncMock(), send_media_group=AsyncMock(), send_message=AsyncMock())
    publisher = TelegramPublisher(bot, chat_id=-1001, signer=TokenSigner("s" * 32), bot_username="testbot", support_url="https://t.me/support")
    ad = make_ad().model_copy(update={"source_description": marker})
    assert is_substandard_structure(ad)
    with pytest.raises(TelegramPublishError, match="Excluded agency"):
        await publisher.publish(77, ad)
    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_other_lalafo_realtors_remain_allowed():
    from app.lalafo.exclusions import is_excluded_agency

    ad = make_ad().model_copy(update={"seller_type": "realtor", "source_description": "Агентство недвижимости. Сдается квартира."})
    assert not is_excluded_agency(ad)


def test_salut_source_parameters_are_excluded():
    from app.lalafo.exclusions import is_excluded_agency

    ad = make_ad().model_copy(update={"source_params": [{"name": "Агентство", "value": "Салют"}]})
    assert is_excluded_agency(ad)


@pytest.mark.asyncio
async def test_three_room_apartment_is_not_sent_to_channel():
    bot = SimpleNamespace(send_photo=AsyncMock(), send_media_group=AsyncMock(), send_message=AsyncMock())
    publisher = TelegramPublisher(bot, chat_id=-1001, signer=TokenSigner("s" * 32), bot_username="testbot", support_url="https://t.me/support")
    with pytest.raises(TelegramPublishError, match="Only studios"):
        await publisher.publish(77, make_ad().model_copy(update={"rooms": "3"}))
    bot.send_photo.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()
    bot.send_message.assert_not_awaited()

@pytest.mark.asyncio
async def test_unknown_author_publishes_without_false_owner_label():
    bot=SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1)]),send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
    publisher=TelegramPublisher(bot,chat_id=-1001,signer=TokenSigner('s'*32),bot_username='testbot',support_url='https://t.me/support')
    ad=make_ad().model_copy(update={'seller_type':'unknown','owner_listing':False})
    await publisher.publish(77,ad)
    args=bot.send_message.await_args.kwargs
    assert args['reply_markup'].inline_keyboard[0][0].text=='Получить номер'
    assert 'Собственник' not in args['text']
    assert ad.seller_type=='unknown' and ad.owner_listing is False


@pytest.mark.asyncio
@pytest.mark.parametrize("rooms,label", [("studio", "Студия"), ("1", "1-комнатная"), ("2", "2-комнатная")])
async def test_publisher_sends_each_supported_room_type(rooms, label):
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=1)]),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=2)))
    publisher = TelegramPublisher(bot, chat_id=-1001, signer=TokenSigner("s" * 32),
        bot_username="testbot", support_url="https://t.me/support")
    result = await publisher.publish(77, make_ad().model_copy(update={"rooms": rooms}))
    assert result.message_id == 2
    assert label in bot.send_message.await_args.kwargs["text"]

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher


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
        max_photos=5,
    )

    result = await publisher.publish(77, make_ad())

    assert result is card
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "Получить номер",
        "Подать заявку на поиск квартиры",
        "🛟 Техподдержка",
    ]
    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == [
        "https://img.example/1.jpg",
        "https://img.example/2.jpg",
    ]
    bot.send_photo.assert_not_awaited()


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
    assert "Автор: собственник" in card["text"]
    assert "Статус:" not in card["text"]
    keyboard = card["reply_markup"]
    assert keyboard.inline_keyboard[0][0].url.startswith(
        "https://t.me/testbot/access?startapp="
    )
    token = keyboard.inline_keyboard[0][0].url.split("startapp=", 1)[1]
    assert signer.decode_public_start_id(token) == 77


@pytest.mark.asyncio
@pytest.mark.parametrize("seller_type,owner_listing", [("unknown", False), ("realtor", False), ("owner", False)])
async def test_unconfirmed_owner_is_rejected_before_sending(seller_type, owner_listing) -> None:
    bot = SimpleNamespace(send_media_group=AsyncMock(), send_message=AsyncMock())
    publisher = TelegramPublisher(
        bot, chat_id=-1001, signer=TokenSigner("s" * 32),
        bot_username="testbot", support_url="https://t.me/support",
    )
    ad = make_ad().model_copy(update={"seller_type": seller_type, "owner_listing": owner_listing})

    with pytest.raises(TelegramPublishError, match="confirmed owner"):
        await publisher.publish(77, ad)

    bot.send_media_group.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_file_id_publish_error_does_not_retry_as_url() -> None:
    bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=RuntimeError("Telegram rejected file ID")),
        send_message=AsyncMock(),
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

    with pytest.raises(TelegramPublishError, match="photo file IDs"):
        await publisher.publish(77, ad)

    bot.send_media_group.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_card_sends_every_photo_across_multiple_albums() -> None:
    card = SimpleNamespace(message_id=30)
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            side_effect=[
                [SimpleNamespace(message_id=index) for index in range(1, 11)],
                [SimpleNamespace(message_id=11), SimpleNamespace(message_id=12)],
            ]
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
        max_photos=5,
    )

    result = await publisher.publish(77, ad)

    assert result is card
    assert bot.send_media_group.await_count == 2
    sent_urls = [
        item.media
        for call in bot.send_media_group.await_args_list
        for item in call.kwargs["media"]
    ]
    assert sent_urls == ad.photo_urls
    bot.send_photo.assert_not_awaited()

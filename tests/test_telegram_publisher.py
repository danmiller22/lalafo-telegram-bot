from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.lalafo.models import LalafoAd
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher


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
    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == [
        "https://img.example/1.jpg",
        "https://img.example/2.jpg",
    ]
    bot.send_photo.assert_not_awaited()


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

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

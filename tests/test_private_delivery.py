from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.telegram.private_delivery import format_private_contact, send_private_contact


def test_private_contact_contains_full_card_and_phone():
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        no_subletting=True,
        phone="+996555123456",
    )

    text = format_private_contact(apartment)

    assert "1-комнатная квартира" in text
    assert "25 000 сом" in text
    assert "+996 555 123 456" in text
    assert "доступен только вам" in text


@pytest.mark.asyncio
async def test_private_contact_repeats_storefront_album_before_contact_card():
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        no_subletting=False,
        phone="+996555123456",
        photo_urls=[f"https://img.example/{index}.jpg" for index in range(8)],
    )
    bot = SimpleNamespace(
        send_media_group=AsyncMock(),
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
    )

    await send_private_contact(
        bot,
        user_id=123,
        apartment=apartment,
        support_url="https://t.me/support",
        max_photos=5,
    )

    media = bot.send_media_group.await_args.args[1]
    assert [item.media for item in media] == apartment.photo_urls[:5]
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert "+996 555 123 456" in bot.send_message.await_args.args[1]
    assert "👥 С подселением" in bot.send_message.await_args.args[1]

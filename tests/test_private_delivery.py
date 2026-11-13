from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import URLInputFile

from app.telegram.private_delivery import (
    PrivatePhotoDeliveryError,
    format_private_contact,
    public_description,
    send_private_contact,
)


def test_public_description_removes_phone_and_telegram_contact():
    text = public_description(
        "Сдаю квартиру. Телефон +996 555 123 456, Telegram @owner_name"
    )
    assert "+996" not in text
    assert "@owner_name" not in text
    assert "контакт доступен после оплаты" in text


def test_private_contact_contains_full_card_and_phone():
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        no_subletting=True,
        phone="+996555123456",
        source_url="https://lalafo.kg/bishkek/ads/example-id-123",
        source_description="Длинное исходное описание",
    )

    text = format_private_contact(apartment)

    assert "1-комнатная квартира" in text
    assert "25 000 сом" in text
    assert "+996 555 123 456" in text
    assert "https://lalafo.kg/bishkek/ads/example-id-123" in text
    assert "Длинное исходное описание" not in text
    assert "Оплата подтверждена" not in text
    assert "доступен только вам" not in text


def test_private_contact_uses_labeled_demo_location_when_district_is_missing():
    apartment = SimpleNamespace(
        rooms="studio",
        district=None,
        city="Бишкек",
        price=22_000,
        deposit=None,
        phone="+996555123456",
    )

    text = format_private_contact(apartment)

    assert "📍 Район не указан\n🏙 Бишкек" in text
    assert "Дем" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_count", [1, 8])
async def test_private_contact_delivers_photos_before_number(photo_count):
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        no_subletting=False,
        phone="+996555123456",
        photo_urls=[f"https://img.example/{index}.jpg" for index in range(photo_count)],
    )
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=index + 1) for index in range(photo_count)]
        ),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        send_message=AsyncMock(),
    )
    async def photos_started(*args, **kwargs):
        bot.send_message.assert_not_awaited()
        if photo_count == 1:
            return SimpleNamespace(message_id=1)
        return [SimpleNamespace(message_id=index + 1) for index in range(photo_count)]

    bot.send_media_group.side_effect = photos_started
    bot.send_photo.side_effect = photos_started

    await send_private_contact(
        bot,
        user_id=123,
        apartment=apartment,
        support_url="https://t.me/support",
        max_photos=5,
    )

    if photo_count == 1:
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.args[1].url == apartment.photo_urls[0]
        bot.send_media_group.assert_not_awaited()
    else:
        media = bot.send_media_group.await_args.args[1]
        assert all(isinstance(item.media, URLInputFile) for item in media)
        assert [item.media.url for item in media] == apartment.photo_urls
        bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert "+996 555 123 456" in bot.send_message.await_args.args[1]
    assert "подсел" not in bot.send_message.await_args.args[1].casefold()


@pytest.mark.asyncio
async def test_private_contact_splits_more_than_ten_photos_without_dropping_any():
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        phone="+996555123456",
        photo_urls=[f"https://img.example/{index}.jpg" for index in range(12)],
    )
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            side_effect=[
                [SimpleNamespace(message_id=index + 1) for index in range(10)],
                [SimpleNamespace(message_id=11), SimpleNamespace(message_id=12)],
            ]
        ),
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

    assert bot.send_media_group.await_count == 2
    sent_urls = [
        item.media.url
        for call in bot.send_media_group.await_args_list
        for item in call.args[1]
    ]
    assert sent_urls == apartment.photo_urls
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_contact_without_photos_sends_no_text() -> None:
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        phone="+996555123456",
        photo_urls=[],
    )
    bot = SimpleNamespace(
        send_media_group=AsyncMock(),
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
    )

    with pytest.raises(PrivatePhotoDeliveryError, match="photos"):
        await send_private_contact(
            bot,
            user_id=123,
            apartment=apartment,
            support_url="https://t.me/support",
        )

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_photo_failure_sends_no_text() -> None:
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        phone="+996555123456",
        photo_urls=["https://img.example/broken.jpg"],
    )
    bot = SimpleNamespace(
        send_media_group=AsyncMock(),
        send_photo=AsyncMock(side_effect=RuntimeError("broken photo")),
        send_message=AsyncMock(),
    )

    with pytest.raises(PrivatePhotoDeliveryError):
        await send_private_contact(
            bot,
            user_id=123,
            apartment=apartment,
            support_url="https://t.me/support",
        )

    bot.send_message.assert_not_awaited()

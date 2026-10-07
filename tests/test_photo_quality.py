from unittest.mock import AsyncMock
from io import BytesIO

import pytest
from PIL import Image

from app.telegram import photo_quality
from app.telegram.photo_quality import WatermarkedPhotos, is_watermark_text


@pytest.mark.parametrize('text', ['www.salut.kg', 'WWW . SALUT . KG', 'salut.kg', 'agency.kg'])
def test_agency_watermark_text(text):
    assert is_watermark_text(text)


@pytest.mark.parametrize('text', ['Samsung', 'Квартира', 'Доступ к контактам'])
def test_ordinary_text_is_not_watermark(text):
    assert not is_watermark_text(text)


@pytest.mark.asyncio
async def test_watermark_rejection_precedes_any_telegram_send(monkeypatch):
    from app.telegram.publisher import TelegramPublisher
    from app.security import TokenSigner
    from tests.test_telegram_publisher import make_ad
    from types import SimpleNamespace
    monkeypatch.setattr('app.telegram.publisher.check_photo_watermarks', AsyncMock(side_effect=WatermarkedPhotos))
    bot = SimpleNamespace(send_photo=AsyncMock(), send_media_group=AsyncMock())
    publisher = TelegramPublisher(bot, chat_id=-1001, signer=TokenSigner('test-secret-long-enough'), bot_username='test', support_url='https://t.me/test')
    with pytest.raises(WatermarkedPhotos):
        await publisher.publish(1, make_ad())
    bot.send_photo.assert_not_called()
    bot.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_manual_file_ids_need_no_external_download(monkeypatch):
    download = AsyncMock(side_effect=AssertionError('Unexpected network request'))
    monkeypatch.setattr(photo_quality.httpx, 'AsyncClient', download)
    await photo_quality.check_photo_watermarks(['AgAC-file-id'])
    download.assert_not_called()

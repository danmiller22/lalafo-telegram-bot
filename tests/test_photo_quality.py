from unittest.mock import AsyncMock
from io import BytesIO

import pytest
from PIL import Image

from app.telegram import photo_quality
from app.telegram.photo_quality import WatermarkedPhotos, is_watermark_text


@pytest.mark.parametrize('text', ['www.salut.kg', 'WWW . SALUT . KG', 'salut.kg', 'agency.kg', 'LOMAEsSerutKC', 'OARSerrulKc'])
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



def test_translucent_watermark_uses_overlapping_line_inspection(monkeypatch, tmp_path):
    from tesserocr import PSM
    import tesserocr
    class FakeOCR:
        def __init__(self, **kwargs):
            self.mode = kwargs["psm"]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def SetImage(self, image):
            pass
        def SetPageSegMode(self, mode):
            self.mode = mode
        def SetVariable(self, *args):
            pass
        def GetUTF8Text(self):
            return "OARSerrulKc" if self.mode == PSM.SINGLE_LINE else ""
    monkeypatch.setattr(tesserocr, "PyTessBaseAPI", FakeOCR)
    monkeypatch.setattr(photo_quality, "_ocr_data_dir", lambda: tmp_path)
    image = Image.new("RGB", (710,950), "gray")
    data = BytesIO()
    image.save(data, format="JPEG")
    assert photo_quality.has_watermark(data.getvalue())



def test_first_async_photo_check_initializes_native_ocr_on_main_thread():
    import subprocess
    import sys
    code = """
import asyncio
import threading
import httpx
from app.telegram import photo_quality
OriginalClient = httpx.AsyncClient

def client(**kwargs):
    return OriginalClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'photo')), **kwargs)

def inspect(data):
    assert threading.current_thread() is not threading.main_thread()
    import tesserocr
    return False

photo_quality.httpx.AsyncClient = client
photo_quality.has_watermark = inspect
asyncio.run(photo_quality.check_photo_watermarks(['https://photos.example/apartment.jpg']))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr

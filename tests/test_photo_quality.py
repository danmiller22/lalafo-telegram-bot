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




@pytest.mark.asyncio
async def test_ocr_worker_results_are_cached_and_parent_does_not_run_native_ocr(monkeypatch, tmp_path):
    monkeypatch.setattr(photo_quality.tempfile, "gettempdir", lambda: str(tmp_path))
    process = type("Process", (), {"returncode": 0, "communicate": AsyncMock(return_value=(b"clean\n", b""))})()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(photo_quality.asyncio, "create_subprocess_exec", spawn)
    assert not await photo_quality.inspect_photo(b"unique photo")
    assert not await photo_quality.inspect_photo(b"unique photo")
    spawn.assert_awaited_once()
    assert spawn.call_args.kwargs["env"]["OMP_THREAD_LIMIT"] == "1"


@pytest.mark.asyncio
async def test_failed_ocr_worker_does_not_cache_clean_result(monkeypatch, tmp_path):
    monkeypatch.setattr(photo_quality.tempfile, "gettempdir", lambda: str(tmp_path))
    process = type("Process", (), {"returncode": 1, "communicate": AsyncMock(return_value=(b"", b"failure"))})()
    monkeypatch.setattr(photo_quality.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(photo_quality.PhotoInspectionError):
        await photo_quality.inspect_photo(b"bad photo")
    assert not list(tmp_path.rglob("*.result"))

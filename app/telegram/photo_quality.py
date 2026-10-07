from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
from io import BytesIO
import re

import httpx
from PIL import Image, ImageOps


class WatermarkedPhotos(ValueError):
    """The original photos contain a visible agency watermark."""


def is_watermark_text(text: str) -> bool:
    compact = re.sub(r'[^a-zа-я0-9.]', '', text.casefold())
    return bool(
        'salut' in compact or 'салют' in compact
        or re.search(r's[ae][lr]{1,2}[uv][lt]{0,2}\.?k[gc]', compact)
        or re.search(r'(?:www\.|[a-z0-9-]+\.(?:kg|com|ru)\b)', compact)
    )


OCR_DATA_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata"
OCR_DATA_SHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"


def _ocr_data_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "arenda-photo-ocr"
    directory.mkdir(exist_ok=True)
    target = directory / "eng.traineddata"
    if not target.exists():
        response = httpx.get(OCR_DATA_URL, timeout=30, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != OCR_DATA_SHA256:
            raise ValueError("Invalid OCR language data")
        temporary = directory / "eng.traineddata.download"
        temporary.write_bytes(data)
        temporary.replace(target)
    return directory


def has_watermark(data: bytes) -> bool:
    from tesserocr import PyTessBaseAPI, PSM
    with Image.open(BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original).convert('L')
        image.thumbnail((1600, 1600))
        with PyTessBaseAPI(path=str(_ocr_data_dir()), lang="eng", psm=PSM.SPARSE_TEXT) as api:
            for candidate in (ImageOps.autocontrast(image), image.point(lambda value: 255 if value >= 185 else 0)):
                api.SetImage(candidate)
                if is_watermark_text(api.GetUTF8Text()):
                    return True
            # Large translucent watermarks are easily lost among furniture
            # textures. Inspect overlapping horizontal strips as single lines.
            api.SetPageSegMode(PSM.SINGLE_LINE)
            api.SetVariable("tessedit_char_whitelist", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.")
            band_height = max(50, round(image.width * 0.127))
            step = max(3, round(image.width * 0.006))
            for top in range(round(image.height * 0.25), round(image.height * 0.75), step):
                band = image.crop((0, top, image.width, min(image.height, top + band_height)))
                api.SetImage(ImageOps.autocontrast(band))
                if is_watermark_text(api.GetUTF8Text()):
                    return True
    return False


async def check_photo_watermarks(urls: list[str]) -> None:
    # Check the whole album before sending its first photo, so a rejected
    # listing cannot leave half an album in the channel.
    if not any(url.startswith(("http://", "https://")) for url in urls):
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in urls:
            if not url.startswith(('http://', 'https://')):
                continue
            async with client.stream('GET', url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 12 * 1024 * 1024:
                        raise ValueError('Photo exceeds watermark inspection limit')
            if await asyncio.to_thread(has_watermark, bytes(data)):
                raise WatermarkedPhotos('Visible agency watermark')

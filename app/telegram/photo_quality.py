from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from contextlib import suppress
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


class PhotoInspectionError(RuntimeError):
    pass


async def inspect_photo(data: bytes) -> bool:
    from app.worker_resources import inventory_worker_lock
    directory = Path(tempfile.gettempdir()) / "arenda-photo-results-v2"
    directory.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    result_path = directory / (digest + ".result")
    if result_path.exists():
        return result_path.read_text() == "watermarked"
    async with inventory_worker_lock:
        if result_path.exists():
            return result_path.read_text() == "watermarked"
        source = directory / (digest + ".image")
        source.write_bytes(data)
        process = None
        try:
            environment = dict(os.environ, OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.telegram.photo_quality", str(source),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
            decision = output.decode().strip()
            if process.returncode or decision not in {"clean", "watermarked"}:
                raise PhotoInspectionError("Photo inspection worker failed")
            result_path.write_text(decision)
            return decision == "watermarked"
        except asyncio.TimeoutError as error:
            raise PhotoInspectionError("Photo inspection timed out") from error
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            source.unlink(missing_ok=True)


async def check_photo_watermarks(urls: list[str]) -> None:
    if not any(url.startswith(("http://", "https://")) for url in urls):
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for url in urls:
            if not url.startswith(('http://', 'https://')):
                continue
            try:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 12 * 1024 * 1024:
                            raise PhotoInspectionError('Photo exceeds inspection limit')
                if await inspect_photo(bytes(data)):
                    raise WatermarkedPhotos('Visible agency watermark')
            except httpx.HTTPError as error:
                raise PhotoInspectionError("Photo could not be downloaded") from error


if __name__ == "__main__":
    print("watermarked" if has_watermark(Path(sys.argv[1]).read_bytes()) else "clean")

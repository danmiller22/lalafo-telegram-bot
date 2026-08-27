from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.lalafo.managed_ads import (
    ManagedAdsContractError,
    clean_jpeg_for_upload,
    image_upload_metadata,
    publication_status,
)


@pytest.mark.parametrize(
    ("content", "advertised", "expected"),
    [
        (b"\xff\xd8\xffdata", "application/octet-stream", ("apartment.jpeg", "image/jpeg")),
        (b"\x89PNG\r\n\x1a\ndata", "binary/octet-stream", ("apartment.png", "image/png")),
        (b"RIFF0000WEBPdata", "image/avif", ("apartment.webp", "image/webp")),
    ],
)
def test_image_upload_metadata_uses_file_signature(content, advertised, expected) -> None:
    assert image_upload_metadata(content, advertised) == expected


def test_image_upload_metadata_rejects_truly_unsupported_image() -> None:
    with pytest.raises(ManagedAdsContractError):
        image_upload_metadata(b"avif bytes", "image/avif")


def test_clean_jpeg_for_upload_reencodes_to_canonical_rgb_jpeg() -> None:
    source = BytesIO()
    Image.new("RGBA", (4, 3), (20, 40, 60, 128)).save(source, format="PNG")
    result = clean_jpeg_for_upload(source.getvalue())
    assert result.startswith(b"\xff\xd8\xff")
    with Image.open(BytesIO(result)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.mode == "RGB"
        assert decoded.size == (4, 3)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status_id": 2}, "active"),
        ({"data": {"status_id": 1}}, "moderation"),
        ({"ad": {"status_id": 11}}, "payment_waiting"),
        ({"status_id": 999}, "unknown"),
        ({"data": []}, "unknown"),
    ],
)
def test_publication_status_uses_lalafo_owner_status(payload, expected) -> None:
    assert publication_status(payload) == expected

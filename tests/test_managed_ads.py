from __future__ import annotations

import pytest

from app.lalafo.managed_ads import (
    ManagedAdsContractError,
    image_upload_metadata,
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

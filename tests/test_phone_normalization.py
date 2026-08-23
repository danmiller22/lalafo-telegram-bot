import pytest

from app.lalafo.phone import display_phone, mask_phone, normalize_kg_phone


@pytest.mark.parametrize(
    "raw",
    ["0555123456", "555123456", "+996555123456", "996555123456"],
)
def test_normalize_phone(raw):
    assert normalize_kg_phone(raw) == "+996555123456"


def test_phone_is_masked_in_logs():
    assert mask_phone("0555123456") == "+996******456"
    assert display_phone("0555123456") == "+996 555 123 456"


def test_invalid_phone_rejected():
    with pytest.raises(ValueError):
        normalize_kg_phone("123")

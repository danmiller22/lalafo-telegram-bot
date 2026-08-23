import pytest

from app.lalafo.deposit import parse_deposit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("депозит 15000", 15000),
        ("Депозит: 15 000 сом", 15000),
        ("аренда + 15000 депозит", 15000),
        ("залог 15000", 15000),
        ("Залог: 15 000", 15000),
        ("без упоминания", None),
        (None, None),
    ],
)
def test_parse_deposit(text, expected):
    assert parse_deposit(text) == expected

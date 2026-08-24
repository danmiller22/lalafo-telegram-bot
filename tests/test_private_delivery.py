from types import SimpleNamespace

from app.telegram.private_delivery import format_private_contact


def test_private_contact_contains_full_card_and_phone():
    apartment = SimpleNamespace(
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=25_000,
        deposit=None,
        phone="+996555123456",
    )

    text = format_private_contact(apartment)

    assert "1-комнатная квартира" in text
    assert "25 000 сом" in text
    assert "+996 555 123 456" in text
    assert "доступен только вам" in text

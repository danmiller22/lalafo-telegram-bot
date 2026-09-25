from app.featured.posting import posting_payload
from tests.helpers import make_ad


def test_requested_lalafo_description_contains_privacy_policy() -> None:
    payload = posting_payload(
        make_ad(price=35_000, district="Филармония", rooms="1")
    )

    description = payload["description"]
    assert "1-комнатная квартира" in description
    assert "Филармония" in description
    assert "Политика конфиденциальности Arenda.KG" in description
    assert "Мы не продаём персональные данные" in description

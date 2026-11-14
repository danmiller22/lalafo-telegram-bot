import pytest
from app.featured.posting import posting_payload, short_description
from app.telegram.formatting import format_apartment
from tests.helpers import make_ad


def test_requested_lalafo_description_contains_only_card_text_and_padding() -> None:
    payload = posting_payload(
        make_ad(price=35_000, district="Филармония", rooms="1")
    )

    description = payload["description"]
    assert "1-комнатная квартира" in description
    assert "Филармония" in description
    expected = format_apartment(make_ad(price=35_000,district="Филармония",rooms="1",deposit=None))
    assert description.startswith(expected)
    assert set(description[len(expected):]) <= {"\n", "."}
    assert len(description) >= 70
    assert "Депозит" not in description
    assert "Полностью меблирована" not in description
    assert "Политика конфиденциальности" not in description


@pytest.mark.parametrize("deposit", [None, 0, 20000])
def test_deposit_is_omitted_without_changing_source_data(deposit):
    ad=make_ad(deposit=deposit)
    assert "депозит" not in short_description(ad).casefold()
    assert ad.deposit == deposit


def test_long_card_is_preserved_without_truncation_or_extra_prose():
    ad=make_ad(district="Токтогула / Суюмбаева, рядом с Восток-5",deposit=None)
    expected=format_apartment(ad)
    assert len(expected)>70
    assert short_description(ad)==expected


def test_selection_example_keeps_its_purpose_in_the_short_card():
    ad=make_ad(source_description="Это пример для подбора квартиры.",deposit=None)
    text=short_description(ad)
    assert text.startswith("🏠 Пример для подбора: 1-комнатная квартира\n")
    assert "💰 Ориентир: 35 000 сом" in text
    assert len(text)>=70

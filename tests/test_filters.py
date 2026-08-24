import pytest

from app.lalafo.parser import is_allowed
from app.telegram.formatting import format_apartment, format_public_apartment
from tests.helpers import make_ad


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"price": 35001}, "price"),
        ({"currency": "USD"}, "wrong_currency"),
        ({"rooms": "3"}, "rooms"),
        ({"city": "Ош"}, "wrong_city"),
        ({"no_subletting": False}, "subletting"),
        ({"photo_urls": []}, "photos"),
        ({"category_id": 1}, "wrong_category"),
    ],
)
def test_filters_reject(overrides, reason):
    allowed, actual = is_allowed(
        make_ad(**overrides), city="Бишкек", max_price=35000, rooms=("studio", "1", "2")
    )
    assert not allowed
    assert actual == reason


def test_allowed_and_format_has_no_source_or_description():
    ad = make_ad()
    assert is_allowed(ad, city="Бишкек", max_price=35000, rooms=("studio", "1", "2"))[0]
    text = format_apartment(ad)
    assert text == (
        "🏠 2-комнатная квартира\n📍 7 мкр\n🏙 Бишкек\n"
        "💰 35 000 сом\n🔐 Депозит: 20 000 сом"
    )
    assert "lalafo" not in text.lower()
    assert ad.phone not in text


def test_agency_listing_is_allowed_but_not_identified_on_card():
    ad = make_ad(owner_listing=False)
    assert is_allowed(ad, city="Бишкек", max_price=35000, rooms=("studio", "1", "2"))[0]
    text = format_apartment(ad)
    assert "риелтор" not in text.casefold()
    assert "собственник" not in text.casefold()


def test_optional_district_and_deposit_are_omitted():
    text = format_apartment(make_ad(district=None, deposit=None, rooms="studio"))
    assert text == "🏠 Студия\n🏙 Бишкек\n💰 35 000 сом"


def test_public_card_has_short_bot_promotion():
    text = format_public_apartment(make_ad(), bot_username="@arenda312bot")
    assert text.endswith("🔎 Ищете квартиру? Подайте заявку: @arenda312bot")

import pytest

from app.lalafo.parser import is_allowed
from app.telegram.formatting import format_apartment, format_public_apartment
from scripts.scrape_publish import (
    candidate_quality,
    is_preferred_district,
    select_balanced_candidates,
)
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


@pytest.mark.parametrize(
    "district",
    ["Филармония", "ЦУМ", "ГУМ", "Восток-5 мкр", "5 мкр", "6 мкр", "7 мкр", "Дордой Плаза"],
)
def test_requested_districts_are_preferred(district):
    assert is_preferred_district(district)


def test_other_numbered_microdistrict_is_not_mistaken_for_fifth():
    assert not is_preferred_district("15 мкр")


def test_quality_prefers_requested_area_then_photo_rich_bargains():
    preferred = make_ad(district="Филармония", photo_urls=["1"], price=30_000)
    elsewhere = make_ad(district="Асанбай", photo_urls=["1", "2"], price=20_000)
    assert candidate_quality(preferred) > candidate_quality(elsewhere)

    bargain = make_ad(district="5 мкр", photo_urls=["1"] * 5, price=20_000)
    expensive = make_ad(district="5 мкр", photo_urls=["1"] * 10, price=35_000)
    sparse = make_ad(district="5 мкр", photo_urls=["1"] * 4, price=10_000)
    assert candidate_quality(bargain) > candidate_quality(expensive)
    assert candidate_quality(bargain) > candidate_quality(sparse)


def test_balanced_selection_uses_70_percent_realtors_and_30_percent_owners():
    candidates = [
        make_ad(
            lalafo_id=index,
            phone=f"+996700000{index:03d}",
            owner_listing=index >= 10,
            price=20_000 + index,
        )
        for index in range(20)
    ]

    selected = select_balanced_candidates(candidates, 10)

    assert len(selected) == 10
    assert sum(not ad.owner_listing for ad in selected) == 7
    assert sum(ad.owner_listing for ad in selected) == 3


def test_balanced_selection_fills_shortage_from_available_group():
    candidates = [
        make_ad(
            lalafo_id=index,
            phone=f"+996701000{index:03d}",
            owner_listing=index == 0,
        )
        for index in range(10)
    ]

    selected = select_balanced_candidates(candidates, 10)

    assert len(selected) == 10
    assert sum(ad.owner_listing for ad in selected) == 1

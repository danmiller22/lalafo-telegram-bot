import pytest

from app.lalafo.parser import is_allowed
from app.telegram.formatting import format_apartment, format_public_apartment
from app.lalafo.subletting import halve_subletting_candidates
from scripts.scrape_publish import (
    candidate_quality,
    is_central_district,
    is_preferred_district,
    select_publish_batch,
    select_publish_batch_with_reposts,
)
from tests.helpers import make_ad


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"price": 40001}, "price"),
        ({"currency": "USD"}, "wrong_currency"),
        ({"rooms": "3"}, "rooms"),
        ({"city": "Ош"}, "wrong_city"),
        ({"photo_urls": []}, "photos"),
        ({"category_id": 1}, "wrong_category"),
    ],
)
def test_filters_reject(overrides, reason):
    allowed, actual = is_allowed(
        make_ad(**overrides), city="Бишкек", max_price=40000, rooms=("studio", "1", "2")
    )
    assert not allowed
    assert actual == reason


def test_allowed_and_format_has_no_source_or_description():
    ad = make_ad()
    assert is_allowed(ad, city="Бишкек", max_price=40000, rooms=("studio", "1", "2"))[0]
    text = format_apartment(ad)
    assert text == (
        "🏠 2-комнатная квартира\n📍 7 мкр\n🏙 Бишкек\n"
        "💰 35 000 сом\n🔐 Депозит: 20 000 сом"
    )
    assert "lalafo" not in text.lower()
    assert ad.phone not in text


def test_agency_listing_is_allowed_but_not_identified_on_card():
    ad = make_ad(owner_listing=False)
    assert is_allowed(ad, city="Бишкек", max_price=40000, rooms=("studio", "1", "2"))[0]
    text = format_apartment(ad)
    assert "риелтор" not in text.casefold()
    assert "собственник" not in text.casefold()


def test_missing_district_and_optional_deposit_are_omitted():
    text = format_apartment(make_ad(district=None, deposit=None, rooms="studio"))
    assert text == "🏠 Студия\n🏙 Бишкек\n💰 35 000 сом"


def test_subletting_listing_is_allowed_but_not_labeled():
    ad = make_ad(no_subletting=False)

    assert is_allowed(ad, city="Бишкек", max_price=40000, rooms=("studio", "1", "2"))[0]
    assert "подсел" not in format_apartment(ad).casefold()


def test_subletting_candidate_supply_is_halved_deterministically():
    whole = [
        make_ad(lalafo_id=100 + index, no_subletting=True)
        for index in range(3)
    ]
    shared = [
        make_ad(lalafo_id=200 + index, no_subletting=False)
        for index in range(6)
    ]

    reduced = halve_subletting_candidates(list(reversed(whole + shared)))

    assert sum(ad.no_subletting for ad in reduced) == 3
    assert sum(not ad.no_subletting for ad in reduced) == 3
    assert {ad.lalafo_id for ad in reduced if not ad.no_subletting} == {
        200, 202, 204
    }


def test_public_card_has_short_bot_promotion():
    text = format_public_apartment(make_ad(), bot_username="@arenda312bot")
    assert text.endswith("🔎 Ищете квартиру? Подайте заявку: @arenda312bot")


@pytest.mark.parametrize(
    "district",
    [
        "Филармония",
        "ЦУМ",
        "ГУМ",
        "Восток-5 мкр",
        "5 мкр",
        "6 мкр",
        "7 мкр",
        "Дордой Плаза",
        "Бишкек Парк",
        "Караван ТЦ",
        "Центр",
        "Золотой квадрат",
        "Площадь Ала-Тоо",
        "Ош базар",
        "Молодая Гвардия",
        "Аламедин-1",
    ],
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


def test_quality_puts_cheap_central_apartment_first():
    central_bargain = make_ad(district="Центр", photo_urls=["1"] * 5, price=25_000)
    central_expensive = make_ad(district="ЦУМ", photo_urls=["1"] * 10, price=39_000)
    cheap_outskirts = make_ad(district="Асанбай", photo_urls=["1"] * 10, price=20_000)

    assert is_central_district(central_bargain.district)
    assert candidate_quality(central_bargain) > candidate_quality(central_expensive)
    assert candidate_quality(central_bargain) > candidate_quality(cheap_outskirts)


def test_publish_batch_targets_seventy_percent_requested_districts():
    preferred = [
        make_ad(lalafo_id=index, district="ЦУМ", phone=f"+996555000{index:03d}")
        for index in range(1, 81)
    ]
    other = [
        make_ad(lalafo_id=100 + index, district="Асанбай", phone=f"+996700000{index:03d}")
        for index in range(1, 81)
    ]

    selected = select_publish_batch(preferred + other, 60)

    assert len(selected) == 60
    assert sum(is_preferred_district(ad.district) for ad in selected) == 42


def test_publish_batch_fills_available_space_when_one_group_is_small():
    preferred = [make_ad(lalafo_id=1, district="ГУМ")]
    other = [make_ad(lalafo_id=10 + index, district="Джал") for index in range(10)]

    selected = select_publish_batch(preferred + other, 8)

    assert len(selected) == 8
    assert preferred[0] in selected


def test_publish_batch_caps_three_hour_repeats_at_five():
    candidates = [
        make_ad(
            lalafo_id=index,
            district="ЦУМ" if index % 2 else "Джал",
            phone=f"+996555{index:06d}",
        )
        for index in range(1, 61)
    ]
    repost_ids = set(range(51, 61))

    selected = select_publish_batch_with_reposts(candidates, repost_ids, 40)

    assert len(selected) == 40
    assert len({ad.lalafo_id for ad in selected} & repost_ids) == 5


def test_publish_batch_uses_fresh_cards_when_repeats_are_unavailable():
    candidates = [
        make_ad(lalafo_id=index, phone=f"+996700{index:06d}")
        for index in range(1, 46)
    ]

    selected = select_publish_batch_with_reposts(candidates, {45}, 40)

    assert len(selected) == 40
    assert sum(ad.lalafo_id == 45 for ad in selected) == 1

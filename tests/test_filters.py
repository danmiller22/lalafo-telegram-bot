from datetime import datetime, timedelta, timezone

import pytest

from app.config import ADDITIONAL_SEARCH_URLS, DEFAULT_SEARCH_URL, Settings
from app.lalafo.parser import is_allowed
from app.telegram.formatting import format_apartment, format_public_apartment
from app.lalafo.subletting import halve_subletting_candidates
from scripts.scrape_publish import (
    CURATED_ROTATION_SPECS,
    CURATED_ROTATION_LALAFO_IDS,
    MAX_REPOSTS_PER_RUN,
    PRIORITY_AD_SPECS,
    SOURCE_ALLOWED_ROOMS,
    SOURCE_MAX_POSTS_PER_RUN,
    SOURCE_MAX_SEARCH_PAGES,
    SOURCE_MIN_PHOTOS,
    SOURCE_MIN_PRICE,
    SOURCE_PUBLISH_SPACING_SECONDS,
    SOURCE_REPOST_AFTER_HOURS,
    TWO_BEDROOM_DAILY_LIMIT,
    TWO_BEDROOM_MAX_PER_RUN,
    TWO_BEDROOM_MAX_PRICE,
    TWO_BEDROOM_MIN_PRICE,
    candidate_quality,
    deduplicate_candidates,
    eligible_curated_apartments,
    is_central_district,
    is_preferred_district,
    minimum_price_for_rooms,
    mix_room_types,
    published_two_bedrooms_today,
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
        make_ad(**{"rooms": "1", **overrides}),
        city="Бишкек",
        max_price=40000,
        rooms=SOURCE_ALLOWED_ROOMS,
    )
    assert not allowed
    assert actual == reason


def test_allowed_and_format_has_no_source_or_description():
    ad = make_ad(rooms="1")
    assert is_allowed(
        ad, city="Бишкек", max_price=40000, rooms=SOURCE_ALLOWED_ROOMS
    )[0]
    text = format_apartment(ad)
    assert text == (
        "🏠 1-комнатная квартира\n📍 7 мкр\n🏙 Бишкек\n"
        "💰 35 000 сом\n🔐 Депозит: 20 000 сом"
    )
    assert "lalafo" not in text.lower()
    assert ad.phone not in text


def test_agency_listing_is_allowed_but_not_identified_on_card():
    ad = make_ad(owner_listing=False, rooms="1")
    assert is_allowed(
        ad, city="Бишкек", max_price=40000, rooms=SOURCE_ALLOWED_ROOMS
    )[0]
    text = format_apartment(ad)
    assert "риелтор" not in text.casefold()
    assert "собственник" not in text.casefold()


def test_missing_district_uses_labeled_demo_location_and_omits_deposit():
    text = format_apartment(make_ad(district=None, deposit=None, rooms="studio"))
    assert text == (
        "🏠 Студия\n📍 Золотой Квадрат\nДем\n🏙 Бишкек\n💰 35 000 сом"
    )


def test_expanded_source_keeps_reposts_strictly_limited():
    settings = Settings(_env_file=None)

    assert SOURCE_ALLOWED_ROOMS == ("1", "studio", "2")
    assert SOURCE_MAX_POSTS_PER_RUN == 18
    assert SOURCE_PUBLISH_SPACING_SECONDS == 150
    assert SOURCE_MAX_SEARCH_PAGES == 36
    assert SOURCE_MIN_PRICE == 10_000
    assert SOURCE_MIN_PHOTOS == 2
    assert MAX_REPOSTS_PER_RUN == 0
    assert SOURCE_REPOST_AFTER_HOURS is None
    assert TWO_BEDROOM_MIN_PRICE == 20_000
    assert TWO_BEDROOM_MAX_PRICE == 40_000
    assert TWO_BEDROOM_DAILY_LIMIT == 20
    assert TWO_BEDROOM_MAX_PER_RUN == 2
    assert settings.rooms == "1"
    assert settings.min_price == 10_000
    assert settings.max_price == 40_000
    assert settings.max_new_posts_per_run == 18
    assert settings.max_search_pages == 36
    assert settings.allow_no_district is True


def test_source_urls_follow_the_operator_filters():
    assert "/1-bedroom/" in DEFAULT_SEARCH_URL
    assert "/owner" in DEFAULT_SEARCH_URL
    assert "/1-bedroom/2-bedrooms/studio/owner/" in DEFAULT_SEARCH_URL
    assert "/semeynym/param-bez-detey/studentam/" in DEFAULT_SEARCH_URL
    assert "/bez-podseleniya/mozhno-s-zhivotnymi" in DEFAULT_SEARCH_URL
    assert "bez-zhivotnyh" not in DEFAULT_SEARCH_URL
    assert "price[from]=10000&price[to]=40000" in DEFAULT_SEARCH_URL
    assert len(ADDITIONAL_SEARCH_URLS) == 1
    supplementary = ADDITIONAL_SEARCH_URLS[0]
    assert "/1-bedroom/2-bedrooms/studio/owner/real-estate-agency" in supplementary
    assert "bez-podseleniya" not in supplementary
    assert "price[from]=10000&price[to]=40000" in supplementary


def test_two_bedroom_price_floor_is_stricter_than_other_rooms():
    assert minimum_price_for_rooms("studio") == 10_000
    assert minimum_price_for_rooms("1") == 10_000
    assert minimum_price_for_rooms("2") == 20_000


def test_room_types_are_interleaved_instead_of_batched():
    cards = [
        make_ad(lalafo_id=1, rooms="1"),
        make_ad(lalafo_id=2, rooms="2"),
        make_ad(lalafo_id=3, rooms="studio"),
        make_ad(lalafo_id=4, rooms="1"),
        make_ad(lalafo_id=5, rooms="2"),
        make_ad(lalafo_id=6, rooms="studio"),
    ]

    assert [ad.rooms for ad in mix_room_types(cards)] == [
        "studio", "2", "1", "studio", "2", "1"
    ]


@pytest.mark.asyncio
async def test_daily_two_bedroom_count_ignores_cheap_and_other_rooms(
    repositories,
):
    apartments, _, sessions = repositories
    valid = await apartments.upsert_discovered(
        make_ad(lalafo_id=9901, rooms="2", price=25_000)
    )
    cheap = await apartments.upsert_discovered(
        make_ad(lalafo_id=9902, rooms="2", price=15_000)
    )
    one_room = await apartments.upsert_discovered(
        make_ad(lalafo_id=9903, rooms="1", price=25_000)
    )
    for message_id, apartment in enumerate((valid, cheap, one_room), start=1):
        await apartments.mark_published(
            apartment.id,
            chat_id=-1001,
            message_id=message_id,
        )

    assert await published_two_bedrooms_today(sessions) == 1


def test_curated_rotation_preserves_manually_approved_apartments():
    assert CURATED_ROTATION_SPECS == (
        ("Моссовет", 20_000),
    )


def test_operator_priority_apartments_have_requested_district_labels():
    assert [int(url.rsplit("-id-", 1)[1]) for url, _ in PRIORITY_AD_SPECS] == [
        115806919,
        115746322,
        116273232,
        114324774,
    ]
    assert [district for _, district in PRIORITY_AD_SPECS] == [
        "1000 мелочей — Дордой Плаза ТЦ",
        "Карпинка — Восток-5",
        "Восток-5",
        "Филармония",
    ]
    assert CURATED_ROTATION_LALAFO_IDS == (
        115333471,
        112925333,
        114091573,
        116107608,
        116136417,
        115936987,
        116040769,
        116159856,
        114533207,
        116120466,
    )


def test_curated_rotation_never_reposts_published_apartments():
    apartments = [
        type("ApartmentStub", (), {"lalafo_id": lalafo_id})()
        for lalafo_id in (101, 102, 103)
    ]

    eligible = eligible_curated_apartments(
        apartments,
        published_ids={101, 102},
        repostable_ids=set(),
    )

    assert [apartment.lalafo_id for apartment in eligible] == [103]


def test_publish_batch_accepts_realtors_without_public_label():
    realtor = make_ad(lalafo_id=1, district="ЦУМ", owner_listing=False)
    owner = make_ad(lalafo_id=2, district="Тунгуч", owner_listing=True)

    selected = select_publish_batch([realtor, owner], limit=2)
    assert {ad.lalafo_id for ad in selected} == {1, 2}
    assert "риелтор" not in format_apartment(realtor).casefold()
    assert "собственник" not in format_apartment(realtor).casefold()


def test_permanently_excluded_source_never_enters_a_publish_batch():
    blocked = make_ad(
        lalafo_id=115809037,
        source_url="https://lalafo.kg/bishkek/ads/example-id-115809037",
        district="ЦУМ",
    )
    allowed = make_ad(lalafo_id=115809038, district="ЦУМ")

    assert select_publish_batch([blocked, allowed], limit=2) == [allowed]


def test_subletting_listing_is_allowed_but_not_labeled():
    ad = make_ad(no_subletting=False, rooms="1")

    assert is_allowed(
        ad, city="Бишкек", max_price=40000, rooms=SOURCE_ALLOWED_ROOMS
    )[0]
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


def test_publish_batch_targets_half_central_half_other_owners():
    preferred = [
        make_ad(lalafo_id=index, district="ЦУМ", phone=f"+996555000{index:03d}")
        for index in range(1, 81)
    ]
    other = [
        make_ad(lalafo_id=100 + index, district="Асанбай", phone=f"+996700000{index:03d}", owner_listing=True)
        for index in range(1, 81)
    ]

    selected = select_publish_batch(preferred + other, 60)

    assert len(selected) == 60
    assert sum(is_central_district(ad.district) for ad in selected) == 30
    assert sum(ad.owner_listing and not is_central_district(ad.district) for ad in selected) == 30


def test_publish_batch_targets_half_central_districts():
    central = [
        make_ad(lalafo_id=index, district="ЦУМ", phone=f"+996555100{index:03d}")
        for index in range(1, 31)
    ]
    preferred = [
        make_ad(
            lalafo_id=100 + index,
            district="Восток-5 мкр",
            phone=f"+996555200{index:03d}",
            owner_listing=True,
        )
        for index in range(1, 31)
    ]
    other = [
        make_ad(lalafo_id=200 + index, district="Асанбай", phone=f"+996555300{index:03d}")
        for index in range(1, 31)
    ]

    selected = select_publish_batch(central + preferred + other, 25)

    assert len(selected) == 25
    assert sum(is_central_district(ad.district) for ad in selected) == 13
    assert sum(ad.owner_listing and not is_central_district(ad.district) for ad in selected) == 12


def test_publish_batch_fills_available_space_when_one_group_is_small():
    preferred = [make_ad(lalafo_id=1, district="ГУМ")]
    other = [make_ad(lalafo_id=10 + index, district="Джал") for index in range(10)]

    selected = select_publish_batch(preferred + other, 8)

    assert len(selected) == 8
    assert preferred[0] in selected


def test_publish_batch_never_contains_the_same_lalafo_ad_twice():
    duplicate = make_ad(lalafo_id=777, district="Тунгуч", phone="+996700000777")
    candidates = [
        duplicate,
        make_ad(lalafo_id=778, district="ЦУМ", phone="+996700000778"),
        duplicate.model_copy(update={"photo_urls": duplicate.photo_urls * 2}),
    ]

    selected = select_publish_batch(candidates, 3)

    assert [ad.lalafo_id for ad in selected].count(777) == 1
    assert len(selected) == 2
    assert len(next(ad for ad in selected if ad.lalafo_id == 777).photo_urls) == 2


def test_publish_batch_excludes_previously_published_ad_entirely():
    duplicate = make_ad(lalafo_id=900, district="Тунгуч", phone="+996700000900")
    selected = select_publish_batch_with_reposts(
        [duplicate, duplicate, make_ad(lalafo_id=901, phone="+996700000901")],
        {900: datetime.now(timezone.utc) - timedelta(days=2)},
        3,
    )

    assert [ad.lalafo_id for ad in selected] == [901]


def test_candidate_deduplication_keeps_the_higher_quality_copy():
    weaker = make_ad(lalafo_id=950, district="Джал", phone="+996700000950")
    stronger = weaker.model_copy(update={"district": "ЦУМ"})

    assert deduplicate_candidates([weaker, stronger]) == [stronger]


def test_publish_batch_uses_fresh_cards_before_any_reposts():
    candidates = [
        make_ad(
            lalafo_id=index,
            district="ЦУМ" if index % 2 else "Джал",
            phone=f"+996555{index:06d}",
        )
        for index in range(1, 61)
    ]
    now = datetime.now(timezone.utc)
    repost_times = {index: now - timedelta(hours=index) for index in range(51, 61)}

    selected = select_publish_batch_with_reposts(candidates, repost_times, 25)

    assert len(selected) == 25
    assert not ({ad.lalafo_id for ad in selected} & set(repost_times))


def test_publish_batch_uses_fresh_cards_when_repeats_are_unavailable():
    candidates = [
        make_ad(lalafo_id=index, phone=f"+996700{index:06d}")
        for index in range(1, 46)
    ]

    selected = select_publish_batch_with_reposts(
        candidates,
        {45: datetime.now(timezone.utc) - timedelta(days=1)},
        25,
    )

    assert len(selected) == 25
    assert sum(ad.lalafo_id == 45 for ad in selected) == 0


def test_publish_batch_does_not_fill_shortage_with_old_posts():
    fresh = [
        make_ad(lalafo_id=index, phone=f"+996700{index:06d}")
        for index in range(1, 11)
    ]
    repeats = [
        make_ad(lalafo_id=100 + index, phone=f"+996701{index:06d}")
        for index in range(1, 31)
    ]
    now = datetime.now(timezone.utc)
    repost_times = {
        ad.lalafo_id: now - timedelta(hours=index)
        for index, ad in enumerate(repeats, start=1)
    }

    selected = select_publish_batch_with_reposts(fresh + repeats, repost_times, 25)
    selected_ids = {ad.lalafo_id for ad in selected}

    assert len(selected) == 10
    assert {ad.lalafo_id for ad in fresh} == selected_ids
    assert not ({ad.lalafo_id for ad in repeats} & selected_ids)


def test_publish_batch_never_adds_any_reposts():
    fresh = [
        make_ad(lalafo_id=index, phone=f"+996702{index:06d}")
        for index in range(1, 11)
    ]
    repeats = [
        make_ad(lalafo_id=100 + index, phone=f"+996703{index:06d}")
        for index in range(1, 31)
    ]
    now = datetime.now(timezone.utc)
    repost_times = {
        ad.lalafo_id: now - timedelta(days=2, minutes=index)
        for index, ad in enumerate(repeats, start=1)
    }

    selected = select_publish_batch_with_reposts(fresh + repeats, repost_times, 40)

    assert len(selected) == 10
    assert not ({ad.lalafo_id for ad in selected} & set(repost_times))

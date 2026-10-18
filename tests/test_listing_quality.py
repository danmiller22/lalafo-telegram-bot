from collections import Counter
import random

import pytest

from app.listing_quality import district_priority, mix_prices, price_bucket
from scripts.scrape_publish import candidate_quality
from tests.helpers import make_ad


@pytest.mark.parametrize("district", ["ЦУМ", "Золотой квадрат", "Моссовет", "Восток-5", "Джал", "Асанбай", "7 мкр"])
def test_requested_good_areas_outrank_unpreferred_outskirts(district):
    assert district_priority(district) > district_priority("Арча-Бешик")


def test_confirmed_revenue_districts_lead_generic_central_areas():
    assert district_priority("Восток-5") > district_priority("Центр")
    assert district_priority("Центр") > district_priority("Золотой квадрат")


def test_lower_rent_wins_when_area_and_photo_quality_match():
    low = make_ad(price=25_000, district="ЦУМ")
    middle = make_ad(price=33_000, district="ЦУМ")
    assert candidate_quality(low) > candidate_quality(middle)
    assert candidate_quality(low) > candidate_quality(make_ad(price=33_000, district="Арча-Бешик"))


def test_mixed_prices_prioritize_thirty_to_thirty_five_thousand():
    cards = [make_ad(lalafo_id=i, price=price) for i, price in enumerate([25_000]*12+[32_000]*12+[39_000]*12)]
    for seed in range(20):
        result = mix_prices(cards, rng=random.Random(seed))
        buckets = [price_bucket(card.price) for card in result]
        assert {card.lalafo_id for card in result} == {card.lalafo_id for card in cards}
        assert Counter(buckets[:6]) == {0: 2, 1: 3, 2: 1}
        assert Counter(buckets[6:12]) == {0: 2, 1: 3, 2: 1}


def test_price_mix_keeps_all_stock_when_one_range_is_short():
    cards = [make_ad(lalafo_id=i, price=price) for i,price in enumerate([25_000]*8+[33_000,39_000])]
    result = mix_prices(cards,rng=random.Random(3))
    assert Counter(card.lalafo_id for card in result) == Counter(card.lalafo_id for card in cards)
    assert {price_bucket(card.price) for card in result[:5]} == {0,1,2}


def test_affordable_center_beats_expensive_east_five_even_with_more_photos():
    cheap = make_ad(price=29_000, district='ЦУМ', rooms='1', photo_urls=['https://img/one'])
    expensive = make_ad(price=40_000, district='Восток-5', photo_urls=['https://img/photo'] * 5)
    assert candidate_quality(cheap) > candidate_quality(expensive)


def test_same_rent_furnished_one_room_beats_unfurnished_or_studio():
    ready = make_ad(price=30_000, rooms='1', source_description='С мебелью и техникой.')
    empty = ready.model_copy(update={'source_description': 'Без мебели.'})
    studio = ready.model_copy(update={'rooms': 'studio'})
    assert candidate_quality(ready) > candidate_quality(empty)
    assert candidate_quality(ready) > candidate_quality(studio)

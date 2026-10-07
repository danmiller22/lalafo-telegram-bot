from collections import Counter
import random

import pytest

from app.listing_quality import district_priority, mix_prices, price_bucket
from scripts.scrape_publish import candidate_quality
from tests.helpers import make_ad


@pytest.mark.parametrize("district", ["ЦУМ", "Золотой квадрат", "Моссовет", "Восток-5", "Джал", "Асанбай", "7 мкр"])
def test_requested_good_areas_outrank_unpreferred_outskirts(district):
    assert district_priority(district) > district_priority("Арча-Бешик")


def test_price_floor_has_no_automatic_priority():
    low = make_ad(price=25_000, district="ЦУМ")
    middle = make_ad(price=33_000, district="ЦУМ")
    assert candidate_quality(middle) > candidate_quality(low)
    assert candidate_quality(low) > candidate_quality(make_ad(price=33_000, district="Арча-Бешик"))


def test_mixed_prices_do_not_form_cheap_then_expensive_blocks():
    cards = [make_ad(lalafo_id=i, price=price) for i, price in enumerate([25_000]*12+[32_000]*12+[39_000]*12)]
    for seed in range(20):
        result = mix_prices(cards, rng=random.Random(seed))
        buckets = [price_bucket(card.price) for card in result]
        assert {card.lalafo_id for card in result} == {card.lalafo_id for card in cards}
        assert all(a != b for a,b in zip(buckets,buckets[1:]))
        assert all(len(set(buckets[i:i+3])) == 3 for i in range(0,len(buckets),3))


def test_price_mix_keeps_all_stock_when_one_range_is_short():
    cards = [make_ad(lalafo_id=i, price=price) for i,price in enumerate([25_000]*8+[33_000,39_000])]
    result = mix_prices(cards,rng=random.Random(3))
    assert Counter(card.lalafo_id for card in result) == Counter(card.lalafo_id for card in cards)
    assert {price_bucket(card.price) for card in result[:3]} == {0,1,2}

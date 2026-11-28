from datetime import datetime, timedelta
import random

from app.inventory import (BISHKEK, MAX_PUBLICATIONS_PER_DAY,
    PUBLICATION_SPACING_MINUTES, plan_period, period_publication_targets)
from tests.test_inventory import _apartments


def test_daily_slots_cover_100_cards_in_existing_window():
    start = datetime(2026, 11, 26, 5, tzinfo=BISHKEK)
    assert MAX_PUBLICATIONS_PER_DAY == 100
    assert PUBLICATION_SPACING_MINUTES * 100 == 21 * 60
    assert period_publication_targets(start)[0] == 50
    assert period_publication_targets(start + timedelta(minutes=630))[0] == 50


def test_realtors_fill_only_missing_owner_slots():
    start = datetime(2026, 11, 26, 5, tzinfo=BISHKEK)
    for owners, expected_owners in [(100, 50), (17, 17), (0, 0)]:
        stock = _apartments(owners, central=True, start_id=1)
        stock += _apartments(100, central=True, start_id=1000, owner=False)
        planned = plan_period(stock, period_start=start, rng=random.Random(1))
        assert len(planned) == 50
        assert sum(p.apartment.seller_type == 'owner' for p in planned) == expected_owners
        assert len({p.apartment.id for p in planned}) == 50

from collections import Counter
from datetime import datetime, timedelta

from app.inventory import BISHKEK, plan_period
from tests.test_inventory import _apartments


def test_daily_plan_balances_sellers_and_excludes_blocked_contacts():
    start = datetime(2026, 11, 19, 5, tzinfo=BISHKEK)
    owners = _apartments(70, central=True, start_id=1000)
    realtors = _apartments(100, central=True, start_id=2000, owner=False)
    realtors[0].phone = "+996 702 133 014"
    first = plan_period(owners + realtors, period_start=start)
    used = {row.apartment.id for row in first}
    second = plan_period(
        [ad for ad in owners + realtors if ad.id not in used],
        period_start=start + timedelta(minutes=630),
    )
    rows = first + second
    assert len({row.apartment.id for row in rows}) == 110
    assert Counter(row.apartment.seller_type for row in rows) == {
        "owner": 55,
        "realtor": 55,
    }
    assert realtors[0].id not in {row.apartment.id for row in rows}

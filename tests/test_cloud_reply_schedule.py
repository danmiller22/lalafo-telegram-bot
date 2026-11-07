from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lalafo.cloud_reply_schedule import cloud_reply_allowed


@pytest.mark.parametrize('month', [1, 7, 10, 12])
@pytest.mark.parametrize('hour', range(24))
def test_pc_hours_are_excluded_in_both_dst_seasons(month, hour):
    local = datetime(2026, month, 15, hour, tzinfo=ZoneInfo('America/Chicago'))
    assert cloud_reply_allowed(local.astimezone(timezone.utc)) == (hour < 7 or hour >= 17)

from datetime import datetime, timedelta, timezone

from scripts.repeat_featured_telegram import is_repeat_window


def test_managed_repeat_runs_in_six_spaced_daily_windows() -> None:
    assert is_repeat_window(datetime(2026, 9, 13, 6, 35, tzinfo=timezone.utc), None)
    assert not is_repeat_window(
        datetime(2026, 9, 13, 7, 35, tzinfo=timezone.utc), None
    )


def test_managed_repeat_requires_global_gap() -> None:
    now = datetime(2026, 9, 13, 6, 35, tzinfo=timezone.utc)
    assert not is_repeat_window(now, now - timedelta(hours=3))
    assert is_repeat_window(now, now - timedelta(hours=4))

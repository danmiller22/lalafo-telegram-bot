from datetime import datetime, timedelta, timezone

from scripts.remote_lalafo_collector import seconds_until_next_collection


def test_collector_waits_for_afternoon_bishkek_run() -> None:
    now = datetime(2026, 9, 23, 7, 30, tzinfo=timezone.utc)  # 13:30 Bishkek

    assert seconds_until_next_collection(now) == 30 * 60


def test_collector_rolls_to_next_morning() -> None:
    now = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)  # 15:00 Bishkek

    assert seconds_until_next_collection(now) == 13 * 60 * 60


def test_collector_handles_exact_slot_without_busy_loop() -> None:
    now = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)  # 14:00 Bishkek

    assert seconds_until_next_collection(now) == 14 * 60 * 60

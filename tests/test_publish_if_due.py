from scripts.publish_if_due import should_publish


def test_manual_run_always_publishes() -> None:
    assert should_publish(force=True, recent_count=80)


def test_primary_or_backup_run_publishes_when_window_is_empty() -> None:
    assert should_publish(force=False, recent_count=0)


def test_backup_run_skips_after_recent_success() -> None:
    assert not should_publish(force=False, recent_count=1)

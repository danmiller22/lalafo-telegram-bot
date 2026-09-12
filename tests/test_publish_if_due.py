from scripts.publish_if_due import cycle_succeeded, should_publish


def test_clean_cycle_with_no_new_unique_cards_is_successful():
    assert cycle_succeeded(exit_code=0, error=None, published=0) is True


def test_failed_empty_cycle_still_requests_recovery():
    assert cycle_succeeded(exit_code=2, error="ExitCode2", published=0) is False


def test_timed_out_cycle_is_successful_only_after_a_durable_publication():
    assert cycle_succeeded(exit_code=2, error="CycleTimeout", published=1) is True
    assert cycle_succeeded(exit_code=2, error="CycleTimeout", published=0) is False


def test_manual_run_always_publishes() -> None:
    assert should_publish(force=True, recent_count=80)


def test_primary_or_backup_run_publishes_when_window_is_empty() -> None:
    assert should_publish(force=False, recent_count=0)


def test_backup_run_skips_after_recent_success() -> None:
    assert not should_publish(force=False, recent_count=1)

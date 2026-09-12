from scripts.notify_admin import private_admin_chat_id


def test_admin_alert_accepts_only_private_telegram_user_id():
    assert private_admin_chat_id(123456789) == 123456789


def test_admin_alert_never_targets_group_channel_or_missing_id():
    assert private_admin_chat_id(-1004389602150) is None
    assert private_admin_chat_id(0) is None

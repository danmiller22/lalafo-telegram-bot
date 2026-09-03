from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

from app.telegram.miniapp import mini_app_html, verify_telegram_init_data


def signed_init_data(*, bot_token: str, user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {
                "id": user_id,
                "first_name": "Айжан",
                "username": "aizhan_test",
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_telegram_init_data_authenticates_user():
    token = "123456:telegram-test-token"
    init_data = signed_init_data(bot_token=token, user_id=778899, auth_date=1_700_000_000)

    user = verify_telegram_init_data(
        init_data,
        bot_token=token,
        now=1_700_000_100,
    )

    assert user is not None
    assert user.id == 778899
    assert user.first_name == "Айжан"
    assert user.username == "aizhan_test"


def test_telegram_init_data_rejects_tampering_and_stale_payload():
    token = "123456:telegram-test-token"
    init_data = signed_init_data(bot_token=token, user_id=778899, auth_date=1_700_000_000)

    assert verify_telegram_init_data(
        init_data.replace("778899", "778898"),
        bot_token=token,
        now=1_700_000_100,
    ) is None
    assert verify_telegram_init_data(
        init_data,
        bot_token=token,
        now=1_700_100_000,
    ) is None


def test_mini_app_page_keeps_payment_and_receipt_in_one_window():
    html = mini_app_html()

    assert "/miniapp/api/session" in html
    assert "/miniapp/api/start" in html
    assert "/miniapp/api/receipt" in html
    assert "Оплатить неделю — 500 сом" in html
    assert "Отправить чек на проверку" in html
    assert '["unpaid", "awaiting_receipt", "rejected"].includes(data.status)' in html
    assert "Без перехода в личный чат" not in html
    assert "команды /start" not in html

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
    assert "/miniapp/api/check" in html
    assert "1 неделя доступа к номерам — 499 сом" in html
    assert "1 месяц доступа к номерам — 999 сом" in html
    assert "Оплатить неделю — 499 сом" in html
    assert "Оплатить месяц — 999 сом" in html
    assert 'id="hero"' not in html
    assert 'id="apartment"' in html
    assert 'id="photos"' in html
    assert 'id="details"' in html
    assert "current.openLink(data.payment_url)" in html
    assert '<script async src="https://telegram.org/js/telegram-web-app.js"></script>' in html
    assert 'query.get("tgWebAppData")' in html
    assert "prepareTelegramContext" in html
    assert "Выберите банк" not in html
    assert "Выберите доступ:" not in html
    assert "Я оплатил(а)" in html
    assert "Статус: оплата проверяется" in html
    assert "Оплата проверяется. Квартира сохранена" in html
    assert 'data.status !== "approved" && data.status !== "pending"' in html
    assert "Открываю Finik" not in html
    assert "Создаём защищённую ссылку" not in html
    assert 'show("plans", !approved)' in html
    assert 'show("status", !approved)' in html
    assert 'show("refresh", !approved' in html
    assert '"\n🔐 Депозит: "' not in html
    assert '"\\n🔐 Депозит: "' in html
    assert "Без перехода в личный чат" not in html
    assert "команды /start" not in html

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path
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


def test_mini_app_page_has_one_lifetime_tariff_and_linked_agreement():
    html = mini_app_html()

    assert "/miniapp/api/session" in html
    assert "/miniapp/api/start" in html
    assert "/miniapp/api/access" in html
    assert "Доступ к контактам навсегда — 699 сом." in html
    assert ">Оплатить 699 сом</button>" in html
    assert "499" not in html and "999" not in html
    assert "Недельный тариф" not in html and "Месячный тариф" not in html
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
    assert 'id="receipt-file"' not in html
    assert "receipt_data" not in html
    assert "Статус: оплата проверяется" not in html
    assert "Нажмите «Я оплатил(а)»." in html
    assert "/miniapp/api/consent" not in html
    assert 'type="checkbox"' not in html
    assert '<dialog id="agreement"' in html
    assert "искусственного интеллекта" in html
    assert "Риелторы и другие лица могут выдавать себя за хозяев" in html
    first_screen = html.split('id="tariff-description"', 1)[1].split('id="checkout"', 1)[0]
    assert first_screen.count("<button") == 1
    assert 'const canPay = !approved && !waiting' in html
    assert 'show("pay-lifetime", canPay)' in html
    assert "Открываю Finik" not in html
    assert "Создаём защищённую ссылку" not in html
    assert 'id="terms"' not in html
    assert 'id="accept"' not in html
    assert 'id="availability"' not in html
    assert 'id="support"' not in html
    assert 'id="privacy"' in html
    assert 'id="refresh"' not in html
    assert '"\n🔐 Депозит: "' not in html
    assert '"\\n🔐 Депозит: "' in html
    assert "Без перехода в личный чат" not in html
    assert "команды /start" not in html


def test_ready_checkout_opens_before_waiting_for_payment_request():
    html = mini_app_html(payment_urls={"lifetime": "https://example.com/pay?x=1"})
    assert '"lifetime": "https://example.com/pay?x=1"' in html
    assert html.index("current.openLink(readyUrl)") < html.index("const data = await paymentStart")
    assert "if (paymentStart) await paymentStart" in html
    assert "setInterval" not in html
    assert "setTimeout(poll, 5000)" in html


def test_checkout_configuration_is_script_safe():
    html = mini_app_html(payment_urls={"lifetime": "https://example.com/</script>"})
    assert "https://example.com/</script>" not in html
    assert "https://example.com/\\u003c/script>" in html


def test_miniapp_checkout_reopening_and_late_status_response():
    if not shutil.which("node"):
        import pytest
        pytest.skip("Node.js is required for the browser script regression checks")
    html = mini_app_html(payment_urls={
        "lifetime": "https://example.com/lifetime",
    })
    subprocess.run(
        ["node", str(Path(__file__).with_name("miniapp_ui_regression.cjs"))],
        input=json.dumps(html), text=True, check=True, capture_output=True, timeout=15,
    )

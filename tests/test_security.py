import re

from app.security import TokenSigner
from app.telegram.keyboards import (
    admin_keyboard,
    apartment_keyboard,
    payment_keyboard,
    private_contact_keyboard,
    private_payment_keyboard,
    reveal_keyboard,
    status_keyboard,
)


def test_signed_callback_and_tampering():
    signer = TokenSigner("a-very-long-test-secret")
    token = signer.sign_id("contact", 152)
    assert signer.verify_id("contact", token) == 152
    assert signer.verify_id("contact", token + "x") is None
    assert signer.verify_id("paid", token) is None


def test_callback_data_is_short_and_contains_no_phone():
    signer = TokenSigner("a-very-long-test-secret")
    support_url = "https://t.me/support_test"
    group = apartment_keyboard(
        123456789,
        signer=signer,
        bot_username="arenda312bot",
        support_url=support_url,
    )
    payment = payment_keyboard(
        123456789,
        signer=signer,
        payment_url="https://qr.finik.kg/payment",
        support_url=support_url,
    )
    admin = admin_keyboard(987654321, signer=signer)
    status = status_keyboard(
        123456789,
        signer=signer,
        payment_url="https://qr.finik.kg/payment",
        support_url=support_url,
    )
    reveal = reveal_keyboard(123456789, signer=signer, support_url=support_url)
    private_payment = private_payment_keyboard(
        123456789,
        signer=signer,
        payment_url="https://qr.finik.kg/payment",
        support_url=support_url,
    )
    private_contact = private_contact_keyboard(support_url=support_url)
    for callback in (
        payment.inline_keyboard[1][0].callback_data,
        status.inline_keyboard[1][0].callback_data,
        reveal.inline_keyboard[0][0].callback_data,
        private_payment.inline_keyboard[1][0].callback_data,
        admin.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][1].callback_data,
    ):
        assert len(callback.encode()) <= 64
        assert "+996" not in callback

    private_url = group.inline_keyboard[0][0].url
    assert private_url.startswith("https://t.me/arenda312bot?start=pay_")
    payload = private_url.split("?start=pay_", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]+", payload)
    assert len(f"pay_{payload}") <= 64
    assert "." not in payload
    assert signer.verify_start_id("payment-link", payload) == 123456789
    assert group.inline_keyboard[1][0].text == "🔎 Подать заявку"
    assert group.inline_keyboard[1][0].url == "https://t.me/arenda312bot?start=want"
    assert payment.inline_keyboard[0][0].url == "https://qr.finik.kg/payment"
    for keyboard in (group, payment, status, reveal, private_payment, private_contact):
        assert keyboard.inline_keyboard[-1][0].text == "🛟 Техподдержка"
        assert keyboard.inline_keyboard[-1][0].url == support_url

    assert [row[0].text for row in private_payment.inline_keyboard] == [
        "💳 Оплатить 100 сом",
        "✅ Проверить оплату",
        "🛟 Техподдержка",
    ]
    assert len(private_contact.inline_keyboard) == 1


def test_payment_and_status_keyboards_keep_recovery_actions():
    signer = TokenSigner("a-very-long-test-secret")
    payment = payment_keyboard(
        7,
        signer=signer,
        payment_url="https://pay.example/7",
        support_url="https://t.me/support_test",
    )
    status = status_keyboard(
        7,
        signer=signer,
        payment_url="https://pay.example/7",
        support_url="https://t.me/support_test",
    )
    assert [row[0].text for row in payment.inline_keyboard] == [
        "💳 Ссылка на оплату",
        "✅ Я оплатил",
        "🔄 Проверить оплату / Получить номер",
        "🛟 Техподдержка",
    ]
    assert [row[0].text for row in status.inline_keyboard] == [
        "💳 Ссылка на оплату",
        "⏳ Проверить оплату / Получить номер",
        "🛟 Техподдержка",
    ]


def test_multi_value_signature_rejects_tampering():
    signer = TokenSigner("a-very-long-test-secret")
    token = signer.sign_values("finik-redirect", 1, 2, 3)
    assert signer.verify_values("finik-redirect", token, count=3) == (1, 2, 3)
    assert signer.verify_values("finik-redirect", token + "x", count=3) is None


def test_start_signature_is_telegram_safe_and_rejects_tampering():
    signer = TokenSigner("a-very-long-test-secret")
    token = signer.sign_start_id("payment-link", 152)

    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert signer.verify_start_id("payment-link", token) == 152
    assert signer.verify_start_id("payment-link", token + "x") is None
    assert signer.verify_start_id("other-purpose", token) is None

from app.security import TokenSigner
from app.telegram.keyboards import (
    admin_keyboard,
    apartment_keyboard,
    payment_keyboard,
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
    contact = apartment_keyboard(
        123456789,
        signer=signer,
        payment_url="https://qr.finik.kg/payment",
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
    for callback in (
        contact.inline_keyboard[0][0].callback_data,
        payment.inline_keyboard[1][0].callback_data,
        status.inline_keyboard[1][0].callback_data,
        reveal.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][1].callback_data,
    ):
        assert len(callback.encode()) <= 64
        assert "+996" not in callback

    assert contact.inline_keyboard[0][0].url is None
    assert payment.inline_keyboard[0][0].url == "https://qr.finik.kg/payment"
    for keyboard in (contact, payment, status, reveal):
        assert keyboard.inline_keyboard[-1][0].text == "🛟 Техподдержка"
        assert keyboard.inline_keyboard[-1][0].url == support_url


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

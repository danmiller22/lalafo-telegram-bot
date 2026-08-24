from app.security import TokenSigner
from app.telegram.keyboards import admin_keyboard, apartment_keyboard


def test_signed_callback_and_tampering():
    signer = TokenSigner("a-very-long-test-secret")
    token = signer.sign_id("contact", 152)
    assert signer.verify_id("contact", token) == 152
    assert signer.verify_id("contact", token + "x") is None
    assert signer.verify_id("paid", token) is None


def test_callback_data_is_short_and_contains_no_phone():
    signer = TokenSigner("a-very-long-test-secret")
    contact = apartment_keyboard(123456789, signer=signer)
    admin = admin_keyboard(987654321, signer=signer)
    for callback in (
        contact.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][1].callback_data,
    ):
        assert len(callback.encode()) <= 64
        assert "+996" not in callback


def test_multi_value_signature_rejects_tampering():
    signer = TokenSigner("a-very-long-test-secret")
    token = signer.sign_values("finik-redirect", 1, 2, 3)
    assert signer.verify_values("finik-redirect", token, count=3) == (1, 2, 3)
    assert signer.verify_values("finik-redirect", token + "x", count=3) is None

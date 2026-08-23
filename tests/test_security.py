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
    contact = apartment_keyboard(123456789, signer=signer, support_url="https://t.me/help")
    admin = admin_keyboard(987654321, signer=signer)
    for callback in (
        contact.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][0].callback_data,
        admin.inline_keyboard[0][1].callback_data,
    ):
        assert len(callback.encode()) <= 64
        assert "+996" not in callback

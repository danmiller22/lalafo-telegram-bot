import pytest

from app.security import TokenSigner
from app.telegram.keyboards import apartment_keyboard, matching_apartment_keyboard, masked_contact_label


@pytest.mark.parametrize('phone,expected', [
    ('+996 (555) 123-456', '+996555123***'),
    ('0555123456', '0555123***'),
    ('', 'Получить номер'),
    (None, 'Получить номер'),
    ('123', 'Получить номер'),
])
def test_mask_last_three_digits(phone, expected):
    assert masked_contact_label(phone) == expected


def test_card_buttons_only_expose_masked_phone_and_keep_signed_target():
    signer = TokenSigner('test-secret-long-enough')
    for factory in (apartment_keyboard, matching_apartment_keyboard):
        kwargs = {'support_url': 'https://t.me/support'} if factory == apartment_keyboard else {}
        markup = factory(123, phone='+996555123456', signer=signer, bot_username='testbot', **kwargs)
        button = markup.inline_keyboard[0][0]
        assert button.text == '+996555123***'
        assert '996555123456' not in markup.model_dump_json()
        assert signer.verify_start_id('miniapp-apartment', button.url.split('startapp=')[1]) == 123

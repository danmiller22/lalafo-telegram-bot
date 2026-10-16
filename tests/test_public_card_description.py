import pytest

from app.telegram.description import SUMMARY_LIMIT, short_public_description
from app.telegram.formatting import format_public_apartment
from tests.helpers import make_ad


@pytest.mark.parametrize('contact', [
    '+996 708 14 56 17', '0708-14-56-17', '708.145.617',
    '0(708)145617', 'о7о8 145 617', 'https://wa.me/996708145617',
    'https://api.whatsapp.com/send?phone=996708145617',
    'tel:+996708145617', '@owner_name', 'owner@example.com',
])
def test_summary_drops_contact_but_keeps_property_facts(contact):
    summary = short_public_description(f'С мебелью, 5 этаж из 9, 40 м².\n{contact}')
    assert summary == 'С мебелью, 5 этаж из 9, 40 м².'


def test_public_card_places_safe_short_description_below_price():
    ad = make_ad(source_description='Есть лифт и балкон.\nТелефон +996 555 123456',
                 deposit=None)
    text = format_public_apartment(ad, bot_username='arenda312bot', wanted_deep_link=True)
    assert text.index('35 000 сом') < text.index('Есть лифт и балкон.') < text.index('Ищете квартиру')
    assert '123456' not in text and '+996' not in text and 'Телефон' not in text


def test_summary_is_bounded_and_does_not_infer_missing_facts():
    text = short_public_description('Квартира с лоджией. ' * 100)
    assert len(text) <= SUMMARY_LIMIT and text.endswith('…')
    assert 'мебель' not in short_public_description(None)


def test_source_html_is_not_rendered_as_a_contact_link():
    ad = make_ad(source_description='<b>С мебелью</b> & уютная. <a href="https://wa.me/996555123456">Написать</a>')
    text = format_public_apartment(ad, bot_username='arenda312bot', wanted_deep_link=True)
    assert 'С мебелью &amp; уютная.' in text
    assert 'wa.me' not in text and '123456' not in text

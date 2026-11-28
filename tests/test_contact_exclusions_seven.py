import pytest
from app.lalafo.exclusions import EXCLUDED_CONTACT_SUFFIXES, is_excluded_contact
from tests.helpers import make_ad

@pytest.mark.parametrize('number', sorted(EXCLUDED_CONTACT_SUFFIXES))
@pytest.mark.parametrize('prefix', ['', '0', '+996', '996'])
@pytest.mark.parametrize('separator', ['', ' ', '.', '-', '(', ')', '\u00a0', '\u2010', '\u2011', '\u2013', '\u2014', '\u2212'])
def test_all_blocked_numbers_in_contact_or_any_description_number(number, prefix, separator):
    value=prefix+separator+separator.join(number)
    assert is_excluded_contact(make_ad(phone=value))
    assert is_excluded_contact(make_ad(phone='+996555123456',
        source_description='Связь +996555654321, второй номер: '+value))

@pytest.mark.parametrize('number', sorted(EXCLUDED_CONTACT_SUFFIXES))
def test_similar_and_longer_numbers_are_not_blocked(number):
    replacement=number[:-1]+str((int(number[-1])+1)%10)
    assert not is_excluded_contact(make_ad(phone='+996'+replacement))
    assert not is_excluded_contact(make_ad(phone='1'+number+'1'))

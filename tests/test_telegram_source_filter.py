from app.telegram.source_filter import extract_kgs_price, is_apartment_offer, is_owner_offer


def test_accepts_owner_rental_under_limit():
    text = "Сдается 1-комн квартира, собственник, 39 000 сом"
    assert is_apartment_offer(text)
    assert is_owner_offer(text)
    assert extract_kgs_price(text) == 39_000


def test_rejects_people_looking_for_housing():
    assert not is_apartment_offer("Ищу квартиру до 30 000 сом")


def test_rejects_over_limit():
    assert not is_apartment_offer("Сдам квартиру, хозяин, 45 000 сом")
    assert not is_apartment_offer("Сдам квартиру, хозяин, 46 000 сом")

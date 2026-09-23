from datetime import datetime, timezone

from app.telegram.sources import parse_telegram_apartments


def _post(text: str, *, post_id: int = 100, timestamp: str = "2026-09-23T06:00:00+00:00") -> str:
    return f"""
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="owners_bishkek/{post_id}">
        <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://img.example/1.jpg')"></a>
        <a class="tgme_widget_message_photo_wrap" style="background-image:url('https://img.example/2.jpg')"></a>
        <div class="tgme_widget_message_text">{text}</div>
        <time datetime="{timestamp}"></time>
      </div>
    </div>
    """


def test_parses_fresh_owner_apartment_with_album_and_contact() -> None:
    html = _post(
        """
        Сдаётся 1-комнатная квартира<br>
        Район: Восток-5<br>
        Аренда: 32 000 сом<br>
        Депозит: 10 000 сом<br>
        Собственник, без посредников<br>
        Телефон: 0705 123 456
        """
    )

    ads = parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    )

    assert len(ads) == 1
    ad = ads[0]
    assert ad.lalafo_id < 0
    assert ad.source_url == "https://t.me/owners_bishkek/100"
    assert ad.rooms == "1"
    assert ad.price == 32_000
    assert ad.deposit == 10_000
    assert ad.district == "Восток-5"
    assert ad.phone == "+996705123456"
    assert ad.owner_listing is True
    assert ad.seller_type == "owner"
    assert len(ad.photo_urls) == 2


def test_rejects_foreign_currency_rent_even_if_som_deposit_is_present() -> None:
    html = _post(
        "Сдаю 2-комнатную квартиру. Оплата 700$ в месяц. "
        "Депозит 20 000 сом. Телефон 0557 050 040"
    )

    assert parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    ) == []


def test_accepts_explicit_kgs_equivalent_from_structured_channel() -> None:
    html = _post(
        "Аренда, квартира, 2-комн. Адрес: Бишкек, ЦУМ. "
        "Цена: $450 (39 500 сом). Тип предложения: от собственника. "
        "Телефон: +996 (555) 55-11-50",
        post_id=104,
    )

    ads = parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    )

    assert len(ads) == 1
    assert ads[0].price == 39_500
    assert ads[0].rooms == "2"
    assert ads[0].owner_listing is True


def test_rejects_search_posts_shared_housing_and_old_cards() -> None:
    search = _post(
        "Ищу квартиру. Нужна 1-комнатная квартира до 30 000 сом. 0705 111 222",
        post_id=101,
    )
    shared = _post(
        "Сдаётся 1-комнатная квартира с подселением. Оплата 25 000 сом. 0705 111 223",
        post_id=102,
    )
    old = _post(
        "Сдаётся 1-комнатная квартира. Цена 25 000 сом. 0705 111 224",
        post_id=103,
        timestamp="2026-09-10T06:00:00+00:00",
    )
    now = datetime(2026, 9, 23, 7, tzinfo=timezone.utc)

    assert parse_telegram_apartments(search, now=now) == []
    assert parse_telegram_apartments(shared, now=now) == []
    assert parse_telegram_apartments(old, now=now) == []

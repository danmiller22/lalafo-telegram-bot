from datetime import datetime, timezone

import pytest

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


def test_structured_rental_card_does_not_require_a_sdam_verb():
    html = _post("Однокомнатная квартира<br>Район: ЦУМ<br>Цена: 32 000 сом<br>Телефон: 0705 123 456")
    ads = parse_telegram_apartments(html, now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc))
    assert len(ads) == 1
    assert ads[0].rooms == "1"
    assert ads[0].price == 32_000


def test_parses_studio_and_uses_center_for_missing_district_above_25k() -> None:
    html = _post(
        "Сдаётся студия. Аренда: 28 000 сом. Собственник. Телефон: 0705 123 457",
        post_id=105,
    )

    ads = parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    )

    assert len(ads) == 1
    assert ads[0].rooms == "studio"
    assert ads[0].district == "Центр"


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


@pytest.mark.parametrize(
    "lodging",
    [
        "гостиница", "отель", "мотель", "хостел", "апарт-отель", "гостевой дом",
        "посуточно", "почасовая аренда", "на сутки", "на ночь", "ночлег",
        "сдаётся номер", "номер со всеми удобствами", "квартирное бюро",
    ],
)
def test_rejects_lodging_business_posts(lodging: str) -> None:
    html = _post(
        f"Сдаётся 1-комнатная квартира, {lodging}. Цена 30 000 сом. "
        "Собственник. 0705 111 225",
        post_id=700,
    )

    assert parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    ) == []


def test_accepts_price_before_label_and_short_room_notation() -> None:
    html = _post(
        "Сдаю 1 ком квартиру на долгий срок. "
        "10.000 депозит, 35.000 оплата. Собственник. "
        "Телефон 0705 450 817",
        post_id=501,
    )

    ads = parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    )

    assert len(ads) == 1
    assert ads[0].rooms == "1"
    assert ads[0].price == 35_000
    assert ads[0].seller_type == "owner"


def test_accepts_thousands_price_notation() -> None:
    html = _post(
        "Сдаётся 1-комнатная квартира. Цена: 32 тыс. сом. "
        "От хозяина. 0555 123 456",
        post_id=502,
    )

    ads = parse_telegram_apartments(
        html,
        now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc),
    )

    assert len(ads) == 1
    assert ads[0].price == 32_000
    assert ads[0].rooms == "1"


def test_accepts_sdam_short_room_and_without_shared_housing():
    html = _post("Сдам 1к квартиру без подселения. Цена 30 000 сом. 0705 123 456")
    ads = parse_telegram_apartments(html, now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc))
    assert len(ads) == 1
    assert ads[0].rooms == "1"
    assert ads[0].phone == "+996705123456"


def test_reads_contact_from_whatsapp_link_without_visible_digits():
    html = _post('Сдам 1-комнатную квартиру. Цена 30 000 сом. '
                 '<a href="https://wa.me/996705123456">Написать WhatsApp</a>')
    ads = parse_telegram_apartments(html, now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc))
    assert len(ads) == 1
    assert ads[0].phone == "+996705123456"


def test_source_diagnostics_explain_rejections_without_contacts():
    from collections import Counter
    stats = Counter()
    html = _post("Сдам 1-комнатную квартиру. Цена 30 000 сом.")
    assert parse_telegram_apartments(html, now=datetime(2026, 9, 23, 7, tzinfo=timezone.utc), diagnostics=stats) == []
    assert stats == {"messages": 1, "missing_phone": 1}


@pytest.mark.asyncio
async def test_newer_agency_flood_does_not_evict_a_confirmed_owner(monkeypatch):
    import httpx
    from app.telegram.sources import fetch_telegram_apartments
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    owner = _post('Сдам 1-комнатную квартиру. Собственник. Цена 30 000 сом. 0705 123 456',
                  post_id=1, timestamp=(now-timedelta(hours=2)).isoformat())
    agencies = ''.join(_post('Сдам 1-комнатную квартиру. Агентство. Цена 30 000 сом. 0705 123 457',
                            post_id=i, timestamp=(now-timedelta(hours=1)).isoformat()) for i in range(2,6))
    async def get(self, url, **kwargs):
        return httpx.Response(200, text=owner+agencies, request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    ads = await fetch_telegram_apartments(('https://t.me/s/owners_bishkek',), limit=2, pages_per_channel=1)
    assert len(ads)==2
    assert ads[0].owner_listing and ads[0].seller_type=='owner'
    assert ads[1].seller_type=='realtor'


@pytest.mark.parametrize('offer', ['Сдается 1ком дом с хозяином один', 'Сдам 1-к дом', 'Сдаю полдома'])
def test_numbered_house_offers_are_not_apartments_or_confirmed_owners(offer):
    html = _post(offer+' Цена 30 000 сом. 0705 123 456')
    assert parse_telegram_apartments(html, now=datetime(2026,9,23,7,tzinfo=timezone.utc))==[]


def test_apartment_in_a_brick_building_remains_eligible():
    html = _post('Сдается 1ком квартира, кирпичный дом. Цена 30 000 сом. Собственник. 0705 123 456')
    ads=parse_telegram_apartments(html,now=datetime(2026,9,23,7,tzinfo=timezone.utc))
    assert len(ads)==1 and ads[0].owner_listing

@pytest.mark.asyncio
async def test_slow_channel_keeps_partial_results_and_does_not_delay_other_sources(monkeypatch):
    import asyncio
    import httpx
    from app.telegram import sources
    now = datetime.now(timezone.utc).isoformat()
    owner = _post('Сдам 1-комнатную квартиру. Собственник. Цена 30 000 сом. 0705 123 456', post_id=1, timestamp=now)
    agency = _post('Сдам 1-комнатную квартиру. Агентство. Цена 35 000 сом. 0705 123 457', post_id=2, timestamp=now)
    async def get(self, url, **kwargs):
        if 'before=' in url:
            await asyncio.Event().wait()
        return httpx.Response(200, text=owner if 'owners' in url else agency, request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient,'get',get)
    monkeypatch.setattr(sources,'CHANNEL_FETCH_BUDGET_SECONDS',0.05)
    ads=await asyncio.wait_for(sources.fetch_telegram_apartments(('https://t.me/s/owners','https://t.me/s/agencies'),pages_per_channel=30),timeout=0.5)
    assert len(ads)==2
    assert {ad.seller_type for ad in ads}=={'owner','realtor'}

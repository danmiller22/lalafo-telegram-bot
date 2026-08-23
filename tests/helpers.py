from __future__ import annotations

from app.lalafo.models import LalafoAd


def make_ad(**overrides) -> LalafoAd:
    values = {
        "lalafo_id": 12345,
        "source_url": "https://lalafo.kg/bishkek/ads/example-id-12345",
        "phone": "+996555123456",
        "price": 35000,
        "currency": "KGS",
        "rooms": "2",
        "district": "7 мкр",
        "city": "Бишкек",
        "deposit": 20000,
        "photo_urls": ["https://img5.lalafo.com/example.jpeg"],
        "category_id": 2044,
        "no_subletting": True,
    }
    values.update(overrides)
    return LalafoAd(**values)

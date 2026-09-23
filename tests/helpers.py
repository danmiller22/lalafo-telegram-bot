from __future__ import annotations

from app.lalafo.models import LalafoAd


def make_ad(**overrides) -> LalafoAd:
    values = {
        "lalafo_id": 12345,
        "source_url": "https://lalafo.kg/bishkek/ads/example-id-12345",
        "phone": "+996555123456",
        "price": 35000,
        "currency": "KGS",
        "rooms": "1",
        "district": "7 мкр",
        "city": "Бишкек",
        "deposit": 20000,
        "photo_urls": ["https://img5.lalafo.com/example.jpeg"],
        "category_id": 2044,
        "no_subletting": True,
        "owner_listing": True,
        "seller_type": "owner",
    }
    values.update(overrides)
    if "owner_listing" in overrides and "seller_type" not in overrides:
        values["seller_type"] = "owner" if overrides["owner_listing"] else "realtor"
    return LalafoAd(**values)

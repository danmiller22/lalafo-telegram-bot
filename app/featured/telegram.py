from __future__ import annotations

from app.lalafo.models import LalafoAd
from app.models import Apartment


def apartment_to_ad(apartment: Apartment) -> LalafoAd:
    return LalafoAd(
        lalafo_id=apartment.lalafo_id,
        source_url=apartment.source_url,
        phone=apartment.phone,
        price=apartment.price,
        currency="KGS",
        rooms=apartment.rooms,
        district=apartment.district,
        city=apartment.city,
        deposit=apartment.deposit,
        photo_urls=list(apartment.photo_urls),
        category_id=2044,
        no_subletting=apartment.no_subletting,
        owner_listing=False,
        source_updated_at=apartment.source_updated_at,
    )

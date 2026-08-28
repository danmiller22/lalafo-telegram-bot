from __future__ import annotations

from datetime import datetime, timezone

from app.featured.posting import has_conflicting_params, posting_payload, posting_preview
from app.featured.selection import select_featured
from app.lalafo.models import LalafoAd


def ad(ad_id: int, *, district: str, price: int, phone: str, photos: int = 5) -> LalafoAd:
    return LalafoAd(
        lalafo_id=ad_id, source_url=f"https://lalafo.kg/{ad_id}", phone=phone,
        price=price, currency="KGS", rooms="1",
        district=district, city="Бишкек", photo_urls=[f"https://img/{ad_id}/{n}" for n in range(photos)],
        category_id=2044, no_subletting=True, owner_listing=False,
        source_updated_at=datetime.now(timezone.utc),
    )


def test_central_under_30000_beats_central_35000() -> None:
    result = select_featured([
        ad(1, district="ЦУМ", price=35_000, phone="1"),
        ad(2, district="ЦУМ", price=29_000, phone="2"),
    ], count=2)
    assert [item.lalafo_id for item in result] == [2, 1]


def test_35000_is_fallback_and_phone_is_unique() -> None:
    result = select_featured([
        ad(1, district="ЦУМ", price=29_000, phone="same"),
        ad(2, district="ГУМ", price=28_000, phone="same"),
        ad(3, district="Филармония", price=35_000, phone="other"),
    ], count=2)
    assert len(result) == 2
    assert len({item.phone for item in result}) == 2
    assert result[-1].lalafo_id == 3


def test_recent_source_is_excluded() -> None:
    result = select_featured([
        ad(1, district="ЦУМ", price=20_000, phone="1"),
        ad(2, district="ГУМ", price=25_000, phone="2"),
    ], recent_source_ids={1})
    assert [item.lalafo_id for item in result] == [2]


def test_payload_selects_every_option_and_leaves_deposit_empty() -> None:
    source = ad(1, district="10 мкр", price=20_000, phone="1")
    source.deposit = 15_000
    payload = posting_payload(source)
    assert not has_conflicting_params(payload)
    assert "phone" not in payload
    assert "source_url" not in payload
    assert payload["hide_phone"] is True
    assert payload["hide_chat"] is False
    params = {item["id"]: item["value"] for item in payload["params"]}
    assert params[357] == 30227
    assert 947 not in params
    assert len(params[948]) == 15
    assert len(params[949]) == 15
    assert len(params[878]) == 12
    assert len(params[870]) == 5
    preview = posting_preview(source)
    assert "только чат Lalafo" in preview
    assert "Депозит: поле оставлено пустым" in preview
    assert "Депозит:" not in payload["description"]
    assert "Фотографий: 5" in preview


def test_payload_keeps_deposit_empty_when_source_deposit_is_missing() -> None:
    source = ad(1, district="ЦУМ", price=27_000, phone="1")
    source.deposit = None
    payload = posting_payload(source)
    params = {item["id"]: item["value"] for item in payload["params"]}
    assert 947 not in params
    assert "Депозит:" not in payload["description"]

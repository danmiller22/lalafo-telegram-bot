from pathlib import Path

from app.state import PostedState, ad_fingerprint, normalized_district, same_listing
from tests.helpers import make_ad


def test_duplicate_detection_by_id_and_fingerprint(tmp_path: Path):
    state = PostedState(path=tmp_path / "state.json", items=[])
    ad = make_ad()
    assert not state.contains(ad.lalafo_id, ad_fingerprint(ad))
    state.add(ad, telegram_message_id=100)
    assert state.contains(ad.lalafo_id)
    same_contact = make_ad(lalafo_id=99999)
    assert state.contains(same_contact.lalafo_id, ad_fingerprint(same_contact))


def test_state_roundtrip_has_no_phone(tmp_path: Path):
    path = tmp_path / "state.json"
    state = PostedState(path=path, items=[])
    state.add(make_ad(), telegram_message_id=100)
    state.save()
    text = path.read_text(encoding="utf-8")
    assert "+996555123456" not in text
    assert PostedState.load(path).contains(12345)


def test_district_spelling_and_reused_photos_identify_same_apartment():
    original = make_ad(
        lalafo_id=1,
        district="Восток-5",
        rooms="studio",
        price=20_000,
        photo_urls=[
            "https://img5.lalafo.com/original/abc123456789.jpeg",
            "https://img5.lalafo.com/original/def123456789.jpeg",
        ],
    )
    repeated = make_ad(
        lalafo_id=2,
        district="Восток-5 мкр",
        rooms="studio",
        price=20_000,
        phone="+996700999999",
        photo_urls=[
            "https://cdn.example/thumb/abc123456789.jpeg?width=400",
            "https://cdn.example/thumb/def123456789.jpeg?width=400",
        ],
    )
    assert normalized_district(original.district) == normalized_district(repeated.district)
    assert same_listing(original, repeated)
    assert same_listing(repeated, original)
    assert not same_listing(
        repeated, original.model_copy(update={"photo_urls": ["unrelated-photo-123.jpg"]})
    )

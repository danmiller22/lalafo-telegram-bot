from pathlib import Path

from app.state import PostedState, ad_fingerprint
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

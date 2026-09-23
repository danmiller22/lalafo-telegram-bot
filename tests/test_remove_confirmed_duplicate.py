from types import SimpleNamespace

from scripts.remove_confirmed_duplicate import (
    JOYKA_ID_MIN,
    confirmed_duplicate_message_ids,
)


def test_only_confirmed_album_can_be_removed():
    photos = [
        f"https://img.example/studia-vostok-5-id-116628443-90460469{index}.jpeg"
        for index in (6, 9, 7, 0)
    ]
    original = SimpleNamespace(
        id=1597,
        lalafo_id=JOYKA_ID_MIN + 532730,
        telegram_chat_id=-1004389602150,
        telegram_message_id=35550,
        phone="+996700123456",
        price=20_000,
        rooms="studio",
        district="Восток-5",
        photo_urls=photos,
    )
    repeated = SimpleNamespace(
        id=1386,
        lalafo_id=116628443,
        telegram_chat_id=-1004389602150,
        telegram_message_id=35555,
        phone=original.phone,
        price=original.price,
        rooms=original.rooms,
        district="Восток-5 мкр",
        photo_urls=photos,
    )
    assert confirmed_duplicate_message_ids(
        original, repeated, chat_id=-1004389602150
    ) == [35551, 35552, 35553, 35554, 35555]
    repeated.telegram_message_id = 35556
    assert confirmed_duplicate_message_ids(
        original, repeated, chat_id=-1004389602150
    ) == []

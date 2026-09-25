from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.bot import lalafo_links
from app.config import Settings
from app.models import Apartment
from app.security import TokenSigner
from tests.helpers import make_ad


class FakeState:
    def __init__(self) -> None:
        self.data = {}
        self.state = None

    async def set_state(self, state) -> None:
        self.state = state

    async def get_state(self) -> str | None:
        return self.state.state if self.state else None

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


def message(text=None, *, user_id=777, username=None, photo=None, album=None):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, username=username),
        chat=SimpleNamespace(type="private"),
        photo=[SimpleNamespace(file_id=photo)] if photo else None,
        media_group_id=album,
        answer=AsyncMock(),
    )


def callback(data, *, user_id=777, username=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username=username),
        message=message(),
        answer=AsyncMock(),
    )


async def fill_card(state, settings, *, rooms="студия", author="собственник"):
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    for file_id in ("file-1", "file-2", "file-3"):
        await lalafo_links.manual_card_photo(
            message(photo=file_id, album="album-1"), state, settings
        )
    await lalafo_links.manual_card_photos_done(message("готово"), state, settings)
    await lalafo_links.manual_card_phone(message("0700 123 456"), state, settings)
    await lalafo_links.manual_card_rooms(message(rooms), state, settings)
    await lalafo_links.manual_card_district(message("Восток-5"), state, settings)
    await lalafo_links.manual_card_price(message("28 000"), state, settings)
    author_message = message(author)
    await lalafo_links.manual_card_author(author_message, state, settings)
    return author_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rooms", "author", "stored_rooms", "seller_type", "owner_listing"),
    [
        ("студия", "собственник", "studio", "owner", True),
        ("1-комнатная", "собственник", "1", "owner", True),
    ],
)
async def test_manual_card_album_confirmation_and_publish(
    monkeypatch, rooms, author, stored_rooms, seller_type, owner_listing
):
    state = FakeState()
    settings = Settings(admin_user_id=777)
    preview = await fill_card(state, settings, rooms=rooms, author=author)
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_confirmation
    assert state.data["photo_urls"] == ["file-1", "file-2", "file-3"]
    assert "28 000 сом" in preview.answer.await_args.args[0]
    assert "📞 +996700123456" in preview.answer.await_args.args[0]
    preview_author = (
        "собственник"
        if seller_type == "owner"
        else lalafo_links.unknown_author_label(
            phone=state.data["phone"],
            price=state.data["price"],
            district=state.data["district"],
            rooms=state.data["rooms"],
        )
    )
    assert "👤 Автор: " + preview_author in preview.answer.await_args.args[0]
    assert "Статус:" not in preview.answer.await_args.args[0]
    assert preview.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"manual:publish:{state.data['nonce']}"
    )

    publish = AsyncMock(return_value=SimpleNamespace(message_id=987))
    monkeypatch.setattr(
        lalafo_links,
        "TelegramPublisher",
        lambda *args, **kwargs: SimpleNamespace(publish=publish),
    )
    apartments = SimpleNamespace(
        upsert_discovered=AsyncMock(return_value=SimpleNamespace(id=42)),
        mark_published=AsyncMock(),
    )
    action = callback(f"manual:publish:{state.data['nonce']}")
    await lalafo_links.publish_manual_card(
        action, state, settings, apartments, SimpleNamespace(), SimpleNamespace()
    )

    ad = apartments.upsert_discovered.await_args.args[0]
    assert ad.rooms == stored_rooms
    assert ad.seller_type == seller_type
    assert ad.owner_listing is owner_listing
    assert ad.photo_urls == ["file-1", "file-2", "file-3"]
    assert ad.phone == "+996700123456"
    from app.telegram.formatting import author_label

    assert author_label(ad) == preview_author
    publish.assert_awaited_once_with(42, ad)
    apartments.mark_published.assert_awaited_once_with(
        42, chat_id=settings.telegram_group_id, message_id=987
    )
    assert state.state is None
    assert "рабочими кнопками" in action.message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_username_never_grants_manual_access():
    state = FakeState()
    settings = Settings(admin_user_id=777, admin_username="owner_name")
    impostor = message("/addcard", user_id=123, username="owner_name")
    await lalafo_links.start_manual_card(impostor, state, settings)
    impostor.answer.assert_not_awaited()
    assert state.state is None

    action = callback("manual:add", user_id=123, username="owner_name")
    await lalafo_links.start_manual_card_button(action, state, settings)
    action.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)
    action.message.answer.assert_not_awaited()
    assert state.state is None

    publish_action = callback("manual:publish:bogus", user_id=123, username="owner_name")
    await lalafo_links.publish_manual_card(
        publish_action, state, settings, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    publish_action.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)


@pytest.mark.asyncio
async def test_cancel_manual_card():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    cancel = message("отмена")
    await lalafo_links.cancel_manual_card(cancel, state, settings)
    assert state.state is None
    cancel.answer.assert_awaited_once_with("Публикация отменена.")


@pytest.mark.asyncio
async def test_individual_photos_and_cancel_button():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    first = message(photo="file-1")
    second = message(photo="file-2")
    await lalafo_links.manual_card_photo(first, state, settings)
    await lalafo_links.manual_card_photo(second, state, settings)
    assert state.data["photo_urls"] == ["file-1", "file-2"]
    assert "Фото добавлено: 2" in second.answer.await_args.args[0]
    await lalafo_links.manual_card_photos_done(message("готово"), state, settings)
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_phone

    action = callback(f"manual:cancel:{state.data['nonce']}")
    await lalafo_links.cancel_manual_card_button(action, state, settings)
    assert state.state is None
    action.message.answer.assert_awaited_once_with("Публикация отменена.")


@pytest.mark.asyncio
async def test_manual_form_can_use_buttons_for_every_choice():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    start = message("/addcard")
    await lalafo_links.start_manual_card(start, state, settings)
    photo_markup = start.answer.await_args.kwargs["reply_markup"]
    done_data = photo_markup.inline_keyboard[0][0].callback_data
    assert done_data == f"manual:photos_done:{state.data['nonce']}"

    await lalafo_links.manual_card_photo(message(photo="file-1", album="album-1"), state, settings)
    too_early = callback(done_data)
    await lalafo_links.manual_card_photos_done_button(too_early, state, settings)
    too_early.answer.assert_awaited_once_with("Нужно минимум 2 фотографии.", show_alert=True)
    await lalafo_links.manual_card_photo(message(photo="file-2", album="album-1"), state, settings)
    done = callback(done_data)
    await lalafo_links.manual_card_photos_done_button(done, state, settings)
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_phone

    phone = message("0700 123 456")
    await lalafo_links.manual_card_phone(phone, state, settings)
    rooms_markup = phone.answer.await_args.kwargs["reply_markup"]
    assert [button.text for button in rooms_markup.inline_keyboard[0]] == [
        "Студия",
        "1 комната",
    ]
    room_data = rooms_markup.inline_keyboard[0][1].callback_data
    assert room_data == f"manual:room:1:{state.data['nonce']}"
    room = callback(room_data)
    await lalafo_links.manual_card_rooms_button(room, state, settings)
    assert state.data["rooms"] == "1"
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_district

    await lalafo_links.manual_card_district(message("Центр"), state, settings)
    price = message("32 000")
    await lalafo_links.manual_card_price(price, state, settings)
    author_markup = price.answer.await_args.kwargs["reply_markup"]
    assert author_markup.inline_keyboard[0][0].text == "Собственник"
    author_data = author_markup.inline_keyboard[0][0].callback_data
    assert author_data == f"manual:author:owner:{state.data['nonce']}"
    author = callback(author_data)
    await lalafo_links.manual_card_author_button(author, state, settings)
    assert state.data["seller_type"] == "owner"
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_confirmation
    assert "👤 Автор: собственник" in author.message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_manual_card_accepts_unknown_author():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    reply = await fill_card(state, settings, author="неизвестно")

    assert state.state == lalafo_links.ManualCardPublish.waiting_for_confirmation
    assert state.data["seller_type"] == "unknown"
    assert "Автор:" not in reply.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_room_text_accepts_one_room_phrase():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    await state.set_state(lalafo_links.ManualCardPublish.waiting_for_rooms)

    await lalafo_links.manual_card_rooms(message("1 комната"), state, settings)

    assert state.data["rooms"] == "1"
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_district


@pytest.mark.asyncio
async def test_choice_buttons_reject_username_and_stale_form():
    state = FakeState()
    settings = Settings(admin_user_id=777, admin_username="owner_name")
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    nonce = state.data["nonce"]
    unauthorized = callback(
        f"manual:photos_done:{nonce}", user_id=123, username="owner_name"
    )
    await lalafo_links.manual_card_photos_done_button(unauthorized, state, settings)
    unauthorized.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_photos

    stale = callback("manual:room:1:old_nonce")
    await lalafo_links.manual_card_rooms_button(stale, state, settings)
    stale.answer.assert_awaited_once_with(
        "Кнопка устарела. Откройте текущую карточку.", show_alert=True
    )
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_photos


@pytest.mark.asyncio
async def test_forwarded_album_photo_reaches_manual_form_before_copy_handler():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await lalafo_links.start_manual_card(message("/addcard"), state, settings)
    forwarded = message(photo="file-1", album="group-1")
    forwarded.forward_origin = SimpleNamespace(message_id=123)
    cancel_handler = next(
        item for item in lalafo_links.manual_card_router.message.handlers
        if item.callback is lalafo_links.cancel_manual_card
    )
    photo_handler = next(
        item for item in lalafo_links.manual_card_router.message.handlers
        if item.callback is lalafo_links.manual_card_photo
    )
    cancel_matches, _ = await cancel_handler.check(
        forwarded, raw_state=state.state.state
    )
    photo_matches, _ = await photo_handler.check(
        forwarded, raw_state=state.state.state
    )
    assert not cancel_matches
    assert photo_matches

    await lalafo_links.manual_card_photo(forwarded, state, settings)
    assert state.data["photo_urls"] == ["file-1"]
    forwarded.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_without_active_form_gets_restart_instruction():
    ready = message("готово")
    await lalafo_links.manual_card_expired_state(ready, Settings(admin_user_id=777))
    assert "/addcard" in ready.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_confirmation_button_cannot_publish_new_card():
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await fill_card(state, settings)
    old_nonce = state.data["nonce"]
    await fill_card(state, settings)
    assert state.data["nonce"] != old_nonce

    action = callback(f"manual:publish:{old_nonce}")
    await lalafo_links.publish_manual_card(
        action, state, settings, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )

    action.answer.assert_awaited_once_with(
        "Карточка уже обработана или отменена.", show_alert=True
    )
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_confirmation


@pytest.mark.asyncio
async def test_publish_failure_can_retry_without_losing_card(monkeypatch):
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await fill_card(state, settings)
    publish = AsyncMock(side_effect=[RuntimeError("Telegram failed"), SimpleNamespace(message_id=9)])
    monkeypatch.setattr(
        lalafo_links,
        "TelegramPublisher",
        lambda *args, **kwargs: SimpleNamespace(publish=publish),
    )
    apartments = SimpleNamespace(
        upsert_discovered=AsyncMock(return_value=SimpleNamespace(id=42)),
        mark_published=AsyncMock(),
    )
    first = callback(f"manual:publish:{state.data['nonce']}")
    await lalafo_links.publish_manual_card(
        first, state, settings, apartments, SimpleNamespace(), SimpleNamespace()
    )
    assert state.state == lalafo_links.ManualCardPublish.waiting_for_confirmation
    assert "повторить" in first.message.answer.await_args.args[0]

    second = callback(f"manual:publish:{state.data['nonce']}")
    await lalafo_links.publish_manual_card(
        second, state, settings, apartments, SimpleNamespace(), SimpleNamespace()
    )
    assert publish.await_count == 2
    assert state.state is None


@pytest.mark.asyncio
async def test_database_failure_after_publish_does_not_offer_retry(monkeypatch):
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await fill_card(state, settings)
    monkeypatch.setattr(
        lalafo_links,
        "TelegramPublisher",
        lambda *args, **kwargs: SimpleNamespace(
            publish=AsyncMock(return_value=SimpleNamespace(message_id=9))
        ),
    )
    apartments = SimpleNamespace(
        upsert_discovered=AsyncMock(return_value=SimpleNamespace(id=42)),
        mark_published=AsyncMock(side_effect=RuntimeError("database failed")),
    )
    action = callback(f"manual:publish:{state.data['nonce']}")
    await lalafo_links.publish_manual_card(
        action, state, settings, apartments, SimpleNamespace(), SimpleNamespace()
    )
    assert state.state is None
    assert "Не публикуйте её повторно" in action.message.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seller_type", "owner_listing"), [("unknown", False), ("owner", True)]
)
async def test_manual_author_status_is_persisted(
    repositories, seller_type, owner_listing
):
    apartments, _, _ = repositories
    ad = make_ad(
        lalafo_id=-987654321,
        source_url="manual://telegram/987654321",
        photo_urls=["file-1", "file-2"],
        seller_type=seller_type,
        owner_listing=owner_listing,
    )

    apartment = await apartments.upsert_discovered(ad, discovery_priority=True)
    stored = await apartments.get(apartment.id)

    assert stored.seller_type == seller_type
    assert stored.owner_listing is owner_listing
    assert stored.photo_urls == ["file-1", "file-2"]


@pytest.mark.asyncio
async def test_confirmed_manual_card_reaches_album_keyboard_and_database(repositories):
    apartments, _, sessions = repositories
    state = FakeState()
    settings = Settings(admin_user_id=777)
    await fill_card(state, settings, rooms="1 комната", author="собственник")
    bot = SimpleNamespace(
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=11),
                SimpleNamespace(message_id=12),
                SimpleNamespace(message_id=13),
            ]
        ),
        send_photo=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=14)),
        delete_message=AsyncMock(),
    )
    action = callback(f"manual:publish:{state.data['nonce']}")

    await lalafo_links.publish_manual_card(
        action, state, settings, apartments, TokenSigner("s" * 32), bot
    )

    assert state.state is None
    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == ["file-1", "file-2", "file-3"]
    card = bot.send_message.await_args.kwargs
    assert "🏠 1-комнатная квартира" in card["text"]
    assert "👤 Автор: собственник" in card["text"]
    assert card["reply_markup"].inline_keyboard[0][0].text == "Получить номер"
    async with sessions() as session:
        stored = (
            await session.scalars(
                select(Apartment).where(Apartment.source_url.like("manual://telegram/%"))
            )
        ).one()
    assert stored.publication_status == "published"
    assert stored.telegram_message_id == 14
    assert stored.seller_type == "owner"

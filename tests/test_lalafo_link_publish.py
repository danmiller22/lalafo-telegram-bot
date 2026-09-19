from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import lalafo_links
from app.config import Settings
from tests.helpers import make_ad


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data.clear()
        self.state = None


def test_extract_lalafo_url_from_message() -> None:
    url = "https://lalafo.kg/bishkek/ads/kvartira-id-116352866?feed_id=5012"

    assert lalafo_links.extract_lalafo_url(f"Закинь эту: {url}.") == url
    assert lalafo_links.extract_lalafo_url("обычный текст") is None
    assert lalafo_links.extract_lalafo_url("https://example.com/ad-id-123") is None


def test_main_and_dedicated_bots_use_separate_routers() -> None:
    assert lalafo_links.main_router is not lalafo_links.router
    assert lalafo_links.main_router.parent_router is None
    assert lalafo_links.router.parent_router is None


def test_repeat_is_blocked_for_48_hours() -> None:
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

    assert lalafo_links.repost_available_at(now - timedelta(hours=47), now=now) == (
        now + timedelta(hours=1)
    )
    assert lalafo_links.repost_available_at(now - timedelta(hours=48), now=now) is None


def test_forwarded_card_fields_are_parsed_from_public_text() -> None:
    assert lalafo_links._forwarded_card_fields(
        "🏠 1-комнатная квартира\n"
        "📍 Восток-5\n"
        "🏙 Бишкек\n"
        "💰 28 000 сом\n\n"
        "🔎 Ищете квартиру? Подайте заявку: @arenda312bot"
    ) == ("1", "Восток-5", 28_000)


@pytest.mark.asyncio
async def test_forwarded_album_is_rebuilt_with_normal_publisher(monkeypatch) -> None:
    apartment = SimpleNamespace(
        id=42,
        photo_urls=["file-1", "file-2", "file-3"],
    )
    answer = AsyncMock()
    messages = [
        SimpleNamespace(answer=AsyncMock()),
        SimpleNamespace(answer=answer),
    ]
    apartments = SimpleNamespace(mark_published=AsyncMock())
    publish = AsyncMock(return_value=SimpleNamespace(message_id=987))

    class FakePublisher:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def publish(self, apartment_id, listing):
            return await publish(apartment_id, listing)

    monkeypatch.setattr(
        lalafo_links,
        "_resolve_forwarded_apartment",
        AsyncMock(return_value=apartment),
    )
    monkeypatch.setattr(lalafo_links, "TelegramPublisher", FakePublisher)
    settings = Settings(admin_user_id=777)

    await lalafo_links._publish_forwarded_batch(
        messages,
        settings=settings,
        apartments=apartments,
        signer=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    publish.assert_awaited_once_with(42, apartment)
    apartments.mark_published.assert_awaited_once_with(
        42,
        chat_id=settings.telegram_group_id,
        message_id=987,
    )
    answer.assert_awaited_once_with(
        "✅ Карточка опубликована альбомом с рабочими кнопками."
    )


@pytest.mark.asyncio
async def test_forwarded_card_resolves_stored_apartment_from_text() -> None:
    origin_date = datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
    apartment = SimpleNamespace(id=42)
    repository = SimpleNamespace(
        get=AsyncMock(),
        get_by_telegram_message=AsyncMock(),
        find_forwarded_card=AsyncMock(return_value=apartment),
    )
    message = SimpleNamespace(
        text=(
            "🏠 2-комнатная квартира\n"
            "📍 Филармония\n"
            "🏙 Бишкек\n"
            "💰 35 000 сом"
        ),
        caption=None,
        reply_markup=None,
        forward_origin=SimpleNamespace(date=origin_date),
    )

    result = await lalafo_links._resolve_forwarded_apartment(
        [message],
        apartments=repository,
        signer=SimpleNamespace(),
    )

    assert result is apartment
    repository.find_forwarded_card.assert_awaited_once_with(
        rooms="2",
        district="Филармония",
        price=35_000,
        origin_date=origin_date,
    )


@pytest.mark.asyncio
async def test_link_prompts_admin_for_district() -> None:
    url = "https://lalafo.kg/bishkek/ads/kvartira-id-116352866"
    message = SimpleNamespace(
        text=url,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(),
    )
    state = FakeState()

    await lalafo_links.request_lalafo_district(
        message,
        state,
        Settings(admin_user_id=777),
    )

    assert state.data == {"source_url": url}
    assert state.state == lalafo_links.ManualLalafoPublish.waiting_for_district
    message.answer.assert_awaited_once_with("Какой район написать в заголовке карточки?")


@pytest.mark.asyncio
async def test_admin_can_publish_a_lalafo_link_with_selected_district(monkeypatch) -> None:
    url = "https://lalafo.kg/bishkek/ads/kvartira-id-116352866"
    ad = make_ad(
        lalafo_id=116352866,
        source_url=url,
        rooms="1",
        price=28_000,
        photo_urls=["https://img/1.jpg", "https://img/2.jpg"],
    )
    answers: list[str] = []
    message = SimpleNamespace(
        text=url,
        from_user=SimpleNamespace(id=777),
        answer=AsyncMock(side_effect=lambda text: answers.append(text)),
    )
    apartments = SimpleNamespace(
        get_by_lalafo=AsyncMock(return_value=None),
        is_duplicate=AsyncMock(return_value=False),
        upsert_discovered=AsyncMock(return_value=SimpleNamespace(id=42)),
        mark_published=AsyncMock(),
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def detail(self, source_url: str):
            assert source_url == url
            return ad

    publish = AsyncMock(return_value=SimpleNamespace(message_id=987))

    class FakePublisher:
        def __init__(self, *args, **kwargs):
            pass

        async def publish(self, apartment_id, listing):
            return await publish(apartment_id, listing)

    monkeypatch.setattr(lalafo_links, "LalafoClient", FakeClient)
    monkeypatch.setattr(lalafo_links, "TelegramPublisher", FakePublisher)
    monkeypatch.setattr(lalafo_links, "find_working_proxies", AsyncMock(return_value=[]))

    settings = Settings(admin_user_id=777)
    await lalafo_links._publish_lalafo_url(
        message,
        url=url,
        district="Восток-5",
        settings=settings,
        apartments=apartments,
        signer=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    published_ad = publish.await_args.args[1]
    assert publish.await_args.args[0] == 42
    assert published_ad.district == "Восток-5"
    apartments.upsert_discovered.assert_awaited_once()
    assert apartments.upsert_discovered.await_args.args[0].district == "Восток-5"
    apartments.mark_published.assert_awaited_once_with(
        42,
        chat_id=settings.telegram_group_id,
        message_id=987,
    )
    assert answers[0] == "⏳ Проверяю квартиру…"
    assert answers[-1] == "✅ Квартира опубликована в группе. ID: 116352866."


@pytest.mark.asyncio
async def test_non_admin_lalafo_link_is_ignored() -> None:
    message = SimpleNamespace(
        text="https://lalafo.kg/bishkek/ads/kvartira-id-12345",
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    apartments = SimpleNamespace()
    state = FakeState()

    await lalafo_links.request_lalafo_district(
        message,
        state,
        Settings(admin_user_id=777),
    )

    message.answer.assert_not_awaited()
    assert state.state is None

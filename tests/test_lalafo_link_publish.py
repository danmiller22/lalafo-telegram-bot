from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import lalafo_links
from app.config import Settings
from tests.helpers import make_ad


def test_extract_lalafo_url_from_message() -> None:
    url = "https://lalafo.kg/bishkek/ads/kvartira-id-116352866?feed_id=5012"

    assert lalafo_links.extract_lalafo_url(f"Закинь эту: {url}.") == url
    assert lalafo_links.extract_lalafo_url("обычный текст") is None
    assert lalafo_links.extract_lalafo_url("https://example.com/ad-id-123") is None


def test_repeat_is_blocked_for_48_hours() -> None:
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

    assert lalafo_links.repost_available_at(now - timedelta(hours=47), now=now) == (
        now + timedelta(hours=1)
    )
    assert lalafo_links.repost_available_at(now - timedelta(hours=48), now=now) is None


@pytest.mark.asyncio
async def test_admin_can_publish_a_lalafo_link(monkeypatch) -> None:
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

    settings = Settings(admin_user_id=777)
    await lalafo_links.publish_lalafo_link(
        message,
        settings,
        apartments,
        signer=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    publish.assert_awaited_once_with(42, ad)
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

    await lalafo_links.publish_lalafo_link(
        message,
        Settings(admin_user_id=777),
        apartments,
        signer=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    message.answer.assert_not_awaited()

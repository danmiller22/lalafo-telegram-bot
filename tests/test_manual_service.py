from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app import manual_service as manual
from app.config import Settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoAccessError
from tests.helpers import make_ad

URL = "https://lalafo.kg/bishkek/ads/kvartira-id-116352866"


def settings(**values):
    return Settings(_env_file=None, admin_user_id=777, callback_secret="x" * 32,
                    lalafo_bot_token="8911032573:test", **values)


def payload(text, update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": 777, "type": "private"},
            "from": {"id": 777}, "text": text}}


@pytest.mark.parametrize("token", ["", "8867149259:wrong"])
def test_manual_service_never_falls_back_to_main_token(token):
    configured = settings(telegram_bot_token="8867149259:main")
    configured.lalafo_bot_token = token
    with pytest.raises(RuntimeError):
        manual.validate_config(configured, "https://manual.example/telegram/manual-webhook", "s" * 32)


@pytest.mark.asyncio
async def test_durable_source_and_duplicate_webhook(monkeypatch):
    engine, sessions = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(manual.metadata.create_all)
        runtime = manual.ManualPublisher(settings(), SimpleNamespace(send_message=AsyncMock()), engine, sessions)
        await runtime.handle(payload(URL))
        restarted = manual.ManualPublisher(settings(), runtime.bot, engine, sessions)
        assert await restarted.source(777) == URL
        assert await restarted.enqueue(payload("Центр")) is True
        assert await restarted.enqueue(payload("Центр")) is False
        monkeypatch.setattr(manual.app.state, "runtime", restarted, raising=False)
        monkeypatch.setattr(manual.app.state, "secret", "s" * 32, raising=False)
        monkeypatch.setattr(manual.app.state, "worker", SimpleNamespace(done=lambda: False), raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=manual.app), base_url="http://test") as client:
            assert (await client.post("/telegram/manual-webhook", json=payload(URL))).status_code == 401
            other = payload(URL, 2)
            other["message"]["from"]["id"] = 888
            response = await client.post("/telegram/manual-webhook", json=other,
                                         headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 32})
            assert response.status_code == 200
        async with engine.connect() as conn:
            assert len((await conn.execute(select(manual.jobs))).all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_district_loads_and_publishes_or_preserves_source_on_403(monkeypatch, blocked):
    engine, sessions = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(manual.metadata.create_all)
        bot = SimpleNamespace(send_message=AsyncMock())
        runtime = manual.ManualPublisher(settings(), bot, engine, sessions)
        runtime.apartments = SimpleNamespace(
            upsert_discovered=AsyncMock(return_value=SimpleNamespace(id=42)),
            mark_published=AsyncMock(),
        )
        ad = make_ad(lalafo_id=116352866, source_url=URL, rooms="1", price=28000,
                     photo_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"])
        class Client:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def detail(self, url):
                assert url == URL
                if blocked:
                    raise LalafoAccessError("403")
                return ad
        publish = AsyncMock(return_value=SimpleNamespace(message_id=99))
        monkeypatch.setattr(manual, "LalafoClient", Client)
        monkeypatch.setattr(manual, "TelegramPublisher", lambda *args, **kwargs: SimpleNamespace(publish=publish))
        await runtime.handle(payload(URL))
        await runtime.handle(payload("Центр", 2))
        if blocked:
            assert await runtime.source(777) == URL
            publish.assert_not_awaited()
            assert "403/429" in bot.send_message.await_args.args[1]
        else:
            assert await runtime.source(777) is None
            assert publish.await_args.args[1].district == "Центр"
            runtime.apartments.mark_published.assert_awaited_once()
    finally:
        await engine.dispose()

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.main import configure_bot_profile


@pytest.mark.asyncio
async def test_bot_profile_removes_command_menu():
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="arenda312bot", id=1)),
        delete_my_commands=AsyncMock(),
    )

    await configure_bot_profile(SimpleNamespace(bot=bot))

    bot.delete_my_commands.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_payment_runtime_excludes_manual_lalafo_router(monkeypatch):
    from app.bot import main

    settings = SimpleNamespace(
        require_callback_secret=lambda: "s" * 32,
        require_bot_token=lambda: "123456:test",
        database_url="sqlite+aiosqlite:///:memory:",
        admin_user_id=777,
    )
    dispatcher = Mock()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "create_engine_and_session", lambda url: (Mock(), Mock()))
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "Bot", Mock())
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))

    await main.create_runtime()

    routers = [call.args[0] for call in dispatcher.include_router.call_args_list]
    assert main.handlers.router in routers
    assert main.lalafo_links.manual_card_router in routers
    assert routers.index(main.lalafo_links.manual_card_router) < routers.index(
        main.lalafo_links.owner_router
    )
    assert main.lalafo_links.main_router not in routers
    assert main.lalafo_links.router not in routers

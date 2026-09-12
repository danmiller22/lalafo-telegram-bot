from types import SimpleNamespace
from unittest.mock import AsyncMock

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

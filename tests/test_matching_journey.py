from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import Settings
from app.matching import handlers
from app.matching.presentation import parse_budget, profile_summary
from app.matching.repository import MatchingRepository
from app.matching.states import ApartmentMatchForm
from app.security import TokenSigner
from tests.helpers import make_ad


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30 000 сом", 30000),
        ("до 35 тыс", 35000),
        ("29,5 тыс.", 29500),
        ("40000", 40000),
        ("30\u00a0000", 30000),
        ("30k", 30000),
        ("25-30 тысяч", None),
        ("2 комнаты 30000", None),
        ("-30000", None),
        ("30.5", None),
        ("9" * 100, None),
    ],
)
def test_budget_accepts_single_amount_without_merging_numbers(text, expected):
    assert parse_budget(text) == expected


@pytest.mark.asyncio
async def test_search_journey_saves_preferences_filters_cards_and_can_resume(repositories):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    for identifier, rooms, price, district in [
        (8101, "1", 29000, "Центр"),
        (8102, "2", 29000, "Центр"),
        (8103, "1", 35000, "Центр"),
        (8104, "1", 28000, "Асанбай"),
    ]:
        await apartments.upsert_discovered(
            make_ad(
                lalafo_id=identifier,
                rooms=rooms,
                price=price,
                district=district,
                seller_type="owner",
                owner_listing=True,
            )
        )
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=505, user_id=505))
    message = SimpleNamespace(
        answer=AsyncMock(), edit_text=AsyncMock(), edit_reply_markup=AsyncMock()
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=505), message=message, answer=AsyncMock()
    )
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await handlers.matching_start(callback, state, matching)
    callback.data = "matching:room:1"
    await handlers.matching_room(callback, state)
    await handlers.matching_rooms_done(callback, state)
    # Defaults need no extra selection; go to concrete districts.
    await handlers.matching_seller_types_done(callback, state, matching)
    options = (await state.get_data())["district_options"]
    callback.data = f"matching:district:{options.index('Центр')}"
    await handlers.matching_district(callback, state)
    await handlers.matching_districts_done(callback, state)
    # Back keeps both rooms and district selections.
    callback.data = "matching:back:districts"
    await handlers.matching_back(callback, state)
    assert (await state.get_data())["matching_districts"] == ["Центр"]
    await handlers.matching_districts_done(callback, state)
    callback.data = "matching:budget:30000"
    await handlers.matching_budget_button(
        callback,
        state,
        matching,
        TokenSigner("a-very-long-test-secret"),
        Settings(_env_file=None),
        bot,
    )
    profile = await matching.profile(505)
    assert profile.rooms == ["1"]
    assert profile.districts == ["Центр"]
    assert profile.max_budget == 30000
    assert await state.get_state() is None
    assert bot.send_photo.await_count == 1
    assert "29 000" in bot.send_photo.await_args.kwargs["caption"]
    assert await matching.matching_apartments(profile) == []  # already delivered
    # "My apartments" deliberately permits revisiting cards already seen.
    callback.data = "matching:mine"
    await handlers.matching_feed(
        callback, matching, TokenSigner("a-very-long-test-secret"), Settings(_env_file=None), bot
    )
    assert bot.send_photo.await_count == 2
    await handlers.matching_start(callback, state, matching)
    assert (await state.get_data())["matching_rooms"] == ["1"]
    assert (await state.get_data())["matching_districts"] == ["Центр"]
    await handlers.matching_home(callback, state, matching)
    assert await state.get_state() is None
    assert (await matching.profile(505)).max_budget == 30000
    await handlers.matching_toggle(callback, matching)
    assert not (await matching.profile(505)).notifications_enabled
    assert "Включить уведомления" in str(message.edit_text.await_args.kwargs["reply_markup"])
    assert "приостановлены" in profile_summary(await matching.profile(505))
    await storage.close()


@pytest.mark.asyncio
async def test_invalid_budget_callback_cannot_save_or_send(repositories):
    _, _, sessions = repositories
    matching = MatchingRepository(sessions)
    callback = SimpleNamespace(
        data="matching:budget:1",
        from_user=SimpleNamespace(id=505),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    await handlers.matching_budget_button(callback, None, matching, None, None, None)
    assert await matching.profile(505) is None
    assert callback.answer.await_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_group_search_link_opens_form_directly(repositories):
    from app.bot.handlers import start_handler

    _, _, sessions = repositories
    matching = MatchingRepository(sessions)
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=505, user_id=505))
    message = SimpleNamespace(
        text="/start search", from_user=SimpleNamespace(id=505), answer=AsyncMock()
    )
    await start_handler(
        message,
        None,
        TokenSigner("a-very-long-test-secret"),
        Settings(_env_file=None),
        None,
        state,
        matching=matching,
    )
    assert await state.get_state() == ApartmentMatchForm.rooms.state
    assert (await state.get_data())["matching_source"] == "group"
    assert message.answer.await_args.args[0].startswith("1/4")
    await storage.close()

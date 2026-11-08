from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Settings
from app.favorites import FavoriteRepository
from app.lalafo.managed_ads import LalafoManagedAdsClient
from app.matching.repository import MatchingRepository
from app.security import TokenSigner
from app.showcase.service import ShowcaseService
from app.showcase.handlers import listing_contact
from app.bot.handlers import start_handler
from app.telegram.formatting import format_showcase_apartment
from app.telegram.keyboards import customer_menu_keyboard, showcase_apartment_keyboard
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_all_my_ads_reads_every_page() -> None:
    client = LalafoManagedAdsClient()
    first = [{"id": index} for index in range(1, 101)]
    second = [{"id": 101}]
    client._json = AsyncMock(side_effect=[{"items": first}, {"items": second}])
    try:
        rows = await client.all_my_ads()
    finally:
        await client.close()

    assert [row["id"] for row in rows] == list(range(1, 102))
    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_showcase_loads_fresh_apartments_from_all_collectors(repositories) -> None:
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=8101))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=8102))
    favorites = FavoriteRepository(sessions)
    settings = Settings(_env_file=None, callback_secret="a-very-long-test-secret")
    matching = SimpleNamespace(current_apartments=AsyncMock(return_value=[first, second]))
    service = ShowcaseService(
        apartments,
        matching,
        favorites,
        settings,
        TokenSigner(settings.callback_secret),
    )

    assert [item.id for item in await service._load_current()] == [first.id, second.id]
    matching.current_apartments.assert_awaited_once_with(limit=500)
    await service.close()


@pytest.mark.asyncio
async def test_showcase_sends_all_cards_five_seconds_apart_then_menu(
    repositories, monkeypatch
) -> None:
    apartments, _, sessions = repositories
    first = await apartments.upsert_discovered(make_ad(lalafo_id=8201))
    second = await apartments.upsert_discovered(make_ad(lalafo_id=8202))
    settings = Settings(
        _env_file=None,
        callback_secret="a-very-long-test-secret",
        showcase_card_interval_seconds=5,
    )
    service = ShowcaseService(
        apartments,
        MatchingRepository(sessions),
        FavoriteRepository(sessions),
        settings,
        TokenSigner(settings.callback_secret),
    )
    service._load_current = AsyncMock(return_value=[first, second])
    service._send_card = AsyncMock()
    service.favorites.saved_ids = AsyncMock(return_value=set())
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.showcase.service.asyncio.sleep", fake_sleep)
    bot = SimpleNamespace(send_message=AsyncMock())

    await service._deliver(bot, 55)

    assert service._send_card.await_count == 2
    assert sleeps == [5, 5]
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.args == (55, "Выберите действие:")
    menu = bot.send_message.await_args.kwargs["reply_markup"]
    assert [row[0].text for row in menu.inline_keyboard] == [
        "🔎 Найти выгодные и дешевые квартиры",
        "✨ Индивидуальный подбор — 1 499 сом",
        "❤️ Избранные",
        "🛟 Техподдержка",
        "ℹ️ О сервисе и услугах",
        "🔒 Политика конфиденциальности",
    ]
    await service.close()


@pytest.mark.asyncio
async def test_favorite_save_is_idempotent(repositories) -> None:
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=8301))
    favorites = FavoriteRepository(sessions)

    assert await favorites.save(77, apartment.id)
    assert not await favorites.save(77, apartment.id)
    assert await favorites.saved_ids(77) == {apartment.id}
    assert [row.id for row in await favorites.apartments(77)] == [apartment.id]
    assert await favorites.remove(77, apartment.id)
    assert await favorites.apartments(77) == []


def test_showcase_card_is_compact_and_has_two_buttons() -> None:
    apartment = SimpleNamespace(
        id=10,
        rooms="1",
        district="Центр",
        city="Бишкек",
        price=35_000,
        deposit=None,
    )
    text = format_showcase_apartment(apartment)
    assert text == (
        "🏠 1-комнатная квартира\n"
        "📍 Центр\n"
        "🏙 Бишкек\n"
        "💰 35 000 сом\n"
        "🔐 Депозит: не указан\n"
        "✅ Актуально на момент публикации"
    )
    forbidden = ("задаток", "Ищете квартиру", "Lalafo", "Телеграм")
    assert not any(value in text for value in forbidden)
    keyboard = showcase_apartment_keyboard(
        apartment.id, signer=TokenSigner("a-very-long-test-secret")
    )
    assert [row[0].text for row in keyboard.inline_keyboard] == [
        "📞 Получить номер",
        "❤️ Сохранить",
    ]
    assert len(customer_menu_keyboard().inline_keyboard) == 6


@pytest.mark.asyncio
async def test_start_launches_showcase_without_intro_message() -> None:
    message = SimpleNamespace(
        text="/start",
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())
    showcase = SimpleNamespace(start=Mock())

    await start_handler(
        message,
        service=None,
        signer=TokenSigner("a-very-long-test-secret"),
        settings=Settings(_env_file=None),
        bot=SimpleNamespace(),
        state=state,
        showcase=showcase,
    )

    state.clear.assert_awaited_once()
    showcase.start.assert_called_once()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_click_shows_terms_before_payment() -> None:
    signer = TokenSigner("a-very-long-test-secret")
    apartment_id = 42
    message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data=f"listing:contact:{signer.sign_id('listing-contact', apartment_id)}",
        from_user=SimpleNamespace(id=123),
        message=message,
        answer=AsyncMock(),
    )
    await listing_contact(
        callback,
        signer=signer,
        showcase=SimpleNamespace(
            current_apartment_ids=AsyncMock(return_value={apartment_id})
        ),
        terms_consents=SimpleNamespace(accepted=AsyncMock(return_value=False)),
        apartments=SimpleNamespace(),
        service=SimpleNamespace(),
        settings=Settings(_env_file=None),
        bot=SimpleNamespace(),
    )

    callback.answer.assert_awaited_once_with()
    assert "Пользовательское соглашение" in message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data.startswith("terms:accept:")

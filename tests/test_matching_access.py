from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import select
from app.matching.access import PaidMatchingMiddleware
from app.matching.repository import MatchingRepository
from app.models import ApartmentMatchDelivery
from app.security import TokenSigner
from app.config import Settings
from tests.helpers import make_ad, grant_personal_for_test

@pytest.mark.asyncio
@pytest.mark.parametrize('access', ['none', 'week', 'expired', 'personal'])
@pytest.mark.parametrize('action', ['matching:start','matching:room:1','matching:budget:30000','matching:mine','matching:more','matching:home'])
async def test_saved_buttons_require_active_personal_access(repositories, monkeypatch, access, action):
    _, _, sessions = repositories
    matching = MatchingRepository(sessions)
    if access != 'none':
        await grant_personal_for_test(sessions, 123, plan='week' if access == 'week' else 'personal', expired=access == 'expired')
    event = AsyncMock(spec=CallbackQuery)
    event.from_user = SimpleNamespace(id=123)
    event.answer = AsyncMock()
    event.data = action
    event.message = SimpleNamespace(answer=AsyncMock())
    handler = AsyncMock(return_value='allowed')
    wall = AsyncMock()
    monkeypatch.setattr('app.matching.access.show_matching_paywall', wall)
    state = SimpleNamespace(clear=AsyncMock())
    result = await PaidMatchingMiddleware()(handler, event, {'matching':matching,'state':state,
        'signer':TokenSigner('long-enough-test-secret'),'settings':Settings(_env_file=None)})
    assert handler.await_count == (1 if access == 'personal' else 0)
    assert wall.await_count == (0 if access == 'personal' else 1)
    if access == 'personal': assert result == 'allowed'
    else: state.clear.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('access', ['none','week','expired'])
async def test_legacy_profiles_cannot_query_enqueue_or_deliver_without_personal(repositories, access):
    apartments, _, sessions = repositories
    matching = MatchingRepository(sessions)
    if access != 'none':
        await grant_personal_for_test(sessions, 123, plan='week' if access == 'week' else 'personal', expired=access == 'expired')
    profile = await matching.save_profile(user_id=123, rooms=['1'], districts=[], all_districts=True,
        max_budget=40000, source='legacy-free')
    apartment = await apartments.upsert_discovered(make_ad())
    await apartments.mark_published(apartment.id, chat_id=-100, message_id=1)
    assert await matching.matching_apartments(profile) == []
    assert await matching.seed_notifications() == 0
    async with sessions.begin() as session:
        session.add(ApartmentMatchDelivery(telegram_user_id=123, apartment_id=apartment.id,
            kind='notification', status='queued', next_attempt_at=datetime.now(timezone.utc)))
    assert await matching.claim_delivery() is None
    async with sessions() as session:
        assert (await session.scalar(select(ApartmentMatchDelivery))).status == 'cancelled'

@pytest.mark.asyncio
async def test_unpaid_customer_can_pause_but_cannot_restart_old_feed(repositories, monkeypatch):
    _, _, sessions = repositories
    matching = MatchingRepository(sessions)
    await matching.save_profile(user_id=123, rooms=['1'], districts=[], all_districts=True,
        max_budget=40000, source='legacy-free')
    event = AsyncMock(spec=CallbackQuery)
    event.from_user = SimpleNamespace(id=123)
    event.answer = AsyncMock()
    event.data = 'matching:toggle'
    event.message = SimpleNamespace(answer=AsyncMock())
    handler = AsyncMock()
    wall = AsyncMock()
    monkeypatch.setattr('app.matching.access.show_matching_paywall', wall)
    data = {'matching':matching,'state':SimpleNamespace(clear=AsyncMock()),
        'signer':TokenSigner('long-enough-test-secret'),'settings':Settings(_env_file=None)}
    await PaidMatchingMiddleware()(handler, event, data)
    handler.assert_awaited_once()
    assert await matching.toggle_notifications(123) is False
    handler.reset_mock()
    await PaidMatchingMiddleware()(handler, event, data)
    handler.assert_not_awaited()
    wall.assert_awaited_once()

@pytest.mark.asyncio
async def test_budget_message_from_stale_form_is_blocked(repositories, monkeypatch):
    _, _, sessions = repositories
    event = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(), text='30000')
    handler = AsyncMock()
    wall = AsyncMock()
    monkeypatch.setattr('app.matching.access.show_matching_paywall', wall)
    await PaidMatchingMiddleware()(handler, event, {'matching':MatchingRepository(sessions),
        'state':SimpleNamespace(clear=AsyncMock()),'signer':TokenSigner('long-enough-test-secret'),
        'settings':Settings(_env_file=None)})
    handler.assert_not_awaited()
    wall.assert_awaited_once()

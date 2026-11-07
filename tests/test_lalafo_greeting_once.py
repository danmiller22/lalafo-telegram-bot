from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import LalafoAutoReplyMeta
from app.lalafo.greeting_claims import GreetingClaims
from app.lalafo.chat_protocol import AUTO_REPLY_TEXT, MessageMeta
from scripts.lalafo_reply_unanswered_once import eligible_latest


def message(id='1', origin='visitor', created=100):
    return MessageMeta(id=id, origin_id=origin, recipient_id='18225388', type=1, kind=1, created_at=created, deleted=False)


@pytest.mark.parametrize('messages,allowed', [
    ([message(created=99)], False),
    ([message(created=101)], True),
    ([message(origin='18225388'), message(id='2', created=102)], False),
    ([], False),
])
def test_only_new_unhandled_conversations_are_eligible(messages, allowed):
    chat=SimpleNamespace(can_send_text=True, is_blocked=False)
    assert bool(eligible_latest(messages, chat, '18225388', 100)) is allowed


@pytest.mark.asyncio
async def test_restart_and_another_inbound_do_not_repeat_an_uncertain_greeting():
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    try:
        async with engine.begin() as connection:
            await connection.run_sync(LalafoAutoReplyMeta.__table__.create)
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        first=GreetingClaims(sessions)
        assert await first.claim('18225388','same-conversation','ack-1')
        await first.finish('18225388','same-conversation','uncertain')
        restarted=GreetingClaims(sessions)
        assert not await restarted.claim('18225388','same-conversation','ack-2')
        assert await restarted.claim('18225388','different-conversation','ack-3')
        assert await restarted.claim('other-account','same-conversation','ack-4')
    finally:
        await engine.dispose()


def test_exact_user_requested_reply():
    assert AUTO_REPLY_TEXT == ('Здравствуйте, да! 👋\n'
        'Подробнее про эту квартиру, а также наши услуги вы можете узнать по ссылке в нашем телеграм:\n\n\n'
        'https://t.me/bishkekarendakg')

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from scripts.lalafo_hourly_18225388 import candidate,ledger_key,history,OWNER
from app.lalafo.chat_protocol import MessageMeta,AUTO_REPLY_TEXT

def message(origin='client',created=101,id='m1'):
    return MessageMeta(id=id,origin_id=origin,recipient_id=OWNER,type=1,kind=1,created_at=created,deleted=False)

def test_reply_policy_preserves_old_history_and_owner_replies():
    chat=SimpleNamespace(can_send_text=True,is_blocked=False)
    assert candidate([message()],chat,100)
    assert not candidate([message(created=99),message(created=102,id='m2')],chat,100)
    assert not candidate([message(origin=OWNER),message(created=102,id='m2')],chat,100)
    assert not candidate([message()],SimpleNamespace(can_send_text=False,is_blocked=False),100)
    assert len(ledger_key('chat'))<=64 and ledger_key('chat')==ledger_key('chat')

def test_exact_reply_text():
    assert AUTO_REPLY_TEXT=='Здравствуйте, да! 👋\nПодробнее про эту квартиру, а также наши услуги вы можете узнать по ссылке в нашем телеграм:\n\n\nhttps://t.me/rentifykg'

@pytest.mark.asyncio
async def test_history_pages_retain_older_owner_message():
    page=[message(created=150+i,id=str(i)) for i in range(26)]
    older=message(origin=OWNER,created=120,id='older')
    client=SimpleNamespace(retrieve_messages=AsyncMock(side_effect=[page,[older]]))
    messages=await history(client,SimpleNamespace())
    assert messages[0]==older
    assert not candidate(messages,SimpleNamespace(can_send_text=True,is_blocked=False),100)

@pytest.mark.asyncio
async def test_truncated_history_is_never_treated_as_unanswered():
    page=[message(created=150+i,id=str(i)) for i in range(26)]
    client=SimpleNamespace(retrieve_messages=AsyncMock(return_value=page))
    with pytest.raises(RuntimeError,match='HistoryPaginationIncomplete'):
        await history(client,SimpleNamespace())

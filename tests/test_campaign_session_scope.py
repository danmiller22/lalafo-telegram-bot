from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select

from app.config import Settings
from app.models import LalafoAutoReplyMeta
from tools import lalafo_campaign_session as transfer


@pytest.mark.asyncio
async def test_campaign_transfer_uses_only_its_scoped_key(repositories,monkeypatch):
    sessions=repositories[2]
    monkeypatch.setattr(transfer,"create_engine_and_session",lambda _: (SimpleNamespace(dispose=AsyncMock()),sessions))
    monkeypatch.setattr(transfer,"get_settings",lambda:Settings(_env_file=None,callback_secret="scoped-secret-for-tests"))
    monkeypatch.setattr(transfer,"KEY_NAME","unrelated-default-key")
    first=await transfer.private_key(create=True,key_name="requested-a")
    await transfer.private_key(create=True,key_name="requested-b")
    public=first.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    envelope=transfer.seal_session(public,{"profile_id":18166976,"token":"test-session"})
    assert (await transfer.load_session(envelope,key_name="requested-a"))["profile_id"]==18166976
    with pytest.raises(ValueError):
        await transfer.load_session(envelope,key_name="requested-b")
    await transfer.retire_session_key(record_completion=True,key_name="requested-a")
    assert await transfer.cutoff_completed(key_name="requested-a")
    assert not await transfer.cutoff_completed(key_name="requested-b")
    async with sessions() as s:
        assert await s.get(LalafoAutoReplyMeta,"requested-a") is None
        assert await s.get(LalafoAutoReplyMeta,"requested-b") is not None
        assert await s.get(LalafoAutoReplyMeta,"unrelated-default-key") is None

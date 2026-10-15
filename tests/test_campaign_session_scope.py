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


@pytest.mark.asyncio
async def test_hosted_transfer_survives_distinct_callback_secrets(repositories, monkeypatch):
    from cryptography.fernet import InvalidToken
    sessions = repositories[2]
    monkeypatch.setattr(transfer, "create_engine_and_session", lambda _: (SimpleNamespace(dispose=AsyncMock()), sessions))
    settings = Settings(_env_file=None, callback_secret="github-secret", database_url="postgresql://runner:shared-private-password@db/arenda")
    monkeypatch.setattr(transfer, "get_settings", lambda: settings)
    monkeypatch.setattr(transfer, "KEY_NAME", "requested_campaign_expiry_key_20261014")
    key = await transfer.private_key(create=True)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    envelope = transfer.seal_session(public, {"profile_id": 17, "token": "test-session"})
    await transfer.rewrap_for_hosted()
    settings.callback_secret = "independent-hosted-secret"
    assert (await transfer.load_session(envelope))["profile_id"] == 17
    settings.database_url = "postgresql://runner:wrong-password@db/arenda"
    with pytest.raises(InvalidToken):
        await transfer.load_session(envelope)

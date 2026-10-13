from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tools.stop_lalafo_campaigns import stop_authorized
from tools.lalafo_campaign_session import open_session, seal_session, storage_cipher


NOW = datetime(2026, 10, 13, 23, 19, 22, tzinfo=timezone.utc)
PLAN = {'owner_profile_id': 17, 'stop_at': NOW.isoformat(), 'campaigns': [{'ad_id': 5, 'campaign_id': 9}]}


def client(details):
    return SimpleNamespace(session=SimpleNamespace(profile_id=17), my_ad_details=AsyncMock(side_effect=details), cancel_campaign=AsyncMock())


def owned(campaigns, owner=17):
    return {'id': 5, 'user_id': owner, 'user_ids': [owner], 'campaign': campaigns}


@pytest.mark.asyncio
async def test_cutoff_cannot_stop_the_paid_day_early():
    c = client([])
    with pytest.raises(RuntimeError, match='before'):
        await stop_authorized(c, PLAN, now=NOW-timedelta(seconds=1))
    c.cancel_campaign.assert_not_awaited()
    c.my_ad_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_verification_performs_no_mutations():
    c = client([owned([{'id': 9, 'status': 'started'}])])
    result = await stop_authorized(c, PLAN, now=NOW-timedelta(days=1), verify_only=True)
    assert result['checked'] == 1 and result['stopped'] == 0
    c.cancel_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_is_scoped_to_the_exact_paid_campaign():
    c = client([owned([{'id': 9, 'status': 'started'}, {'id': 99, 'status': 'started'}]), owned([{'id': 9, 'status': 'stopped'}, {'id': 99, 'status': 'started'}])])
    result = await stop_authorized(c, PLAN, now=NOW)
    assert result['stopped'] == 1
    c.cancel_campaign.assert_awaited_once_with('9')


@pytest.mark.asyncio
async def test_new_unrelated_promotion_is_not_cancelled():
    c = client([owned([{'id': 99, 'status': 'started'}])])
    result = await stop_authorized(c, PLAN, now=NOW)
    assert result['already_finished'] == 1
    c.cancel_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_owner_blocks_cancellation():
    c = client([owned([{'id': 9, 'status': 'started'}], owner=18)])
    with pytest.raises(RuntimeError, match='ownership'):
        await stop_authorized(c, PLAN, now=NOW)
    c.cancel_campaign.assert_not_awaited()


def test_session_envelope_requires_the_private_key_and_storage_secret():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    secret_session = {'profile_id': 17, 'token': 'private-token', 'access_token': 'private-access-token'}
    envelope = seal_session(pem, secret_session)
    assert 'private-token' not in str(envelope)
    assert open_session(key, envelope) == secret_session
    with pytest.raises(InvalidToken):
        storage_cipher('wrong').decrypt(storage_cipher('correct').encrypt(b'private-key'))


@pytest.mark.asyncio
async def test_concurrent_cutoff_is_accepted_only_after_confirmed_cancellation():
    c = client([owned([{'id': 9, 'status': 'started'}]), owned([])])
    c.cancel_campaign.side_effect = RuntimeError('campaign already cancelled')
    result = await stop_authorized(c, PLAN, now=NOW)
    assert result['already_finished'] == 1


@pytest.mark.asyncio
async def test_failed_cancellation_of_still_active_campaign_is_not_hidden():
    c = client([owned([{'id': 9, 'status': 'started'}])]*2)
    c.cancel_campaign.side_effect = RuntimeError('request failed')
    with pytest.raises(RuntimeError, match='request failed'):
        await stop_authorized(c, PLAN, now=NOW)


@pytest.mark.asyncio
async def test_confirmed_retry_does_not_reopen_the_retired_session(monkeypatch):
    from tools import stop_lalafo_campaigns, lalafo_campaign_session
    monkeypatch.setattr(stop_lalafo_campaigns, 'now_utc', lambda: NOW)
    completed = AsyncMock(return_value=True)
    auth = AsyncMock()
    monkeypatch.setattr(lalafo_campaign_session, 'cutoff_completed', completed)
    monkeypatch.setattr(stop_lalafo_campaigns, 'authenticated_client', auth)
    await stop_lalafo_campaigns.run({**PLAN, 'completion_marker': True})
    auth.assert_not_awaited()

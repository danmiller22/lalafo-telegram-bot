from datetime import datetime, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.lalafo.cloud_reply_schedule import cloud_reply_allowed


@pytest.mark.parametrize('month', [1, 7, 10, 12])
@pytest.mark.parametrize('hour', range(24))
def test_pc_hours_are_excluded_in_both_dst_seasons(month, hour):
    local = datetime(2026, month, 15, hour, tzinfo=ZoneInfo('America/Chicago'))
    assert cloud_reply_allowed(local.astimezone(timezone.utc)) == (hour < 7 or hour >= 17)


@pytest.mark.asyncio
async def test_cloud_skip_happens_before_credentials_or_lalafo_access(monkeypatch):
    from scripts import lalafo_reply_unanswered_once as module
    client = AsyncMock()
    monkeypatch.setattr(module, 'cloud_reply_allowed', lambda: False)
    monkeypatch.setattr(module, 'LalafoChatClient', client)
    monkeypatch.delenv('LALAFO_SESSION_ENVELOPE', raising=False)
    result = await module.run_once()
    assert result == {'status': 'skipped', 'reason': 'pc_auto_reply_window', 'sent': 0, 'verified': 0}
    client.assert_not_called()

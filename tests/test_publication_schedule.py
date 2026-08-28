from __future__ import annotations

import pytest

from app.publication_schedule import (
    claim_publication,
    finish_publication,
    schedule_snapshot,
)


@pytest.mark.asyncio
async def test_shared_schedule_allows_only_one_active_publisher(repositories) -> None:
    _, _, sessions = repositories
    first = await claim_publication(
        sessions,
        force=False,
        interval_minutes=180,
        lease_seconds=300,
    )
    assert first is not None

    overlapping = await claim_publication(
        sessions,
        force=True,
        interval_minutes=180,
        lease_seconds=300,
    )
    assert overlapping is None

    assert await finish_publication(
        sessions,
        token=first.token,
        success=True,
        published_count=40,
        error=None,
    )
    snapshot = await schedule_snapshot(sessions, interval_minutes=180)
    assert snapshot.status == "succeeded"
    assert snapshot.last_published_count == 40
    assert not snapshot.due

    too_early = await claim_publication(
        sessions,
        force=False,
        interval_minutes=180,
        lease_seconds=300,
    )
    assert too_early is None

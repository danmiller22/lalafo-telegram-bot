from __future__ import annotations

import pytest

from scripts import process_featured_review_updates, suggest_featured_lalafo


@pytest.mark.asyncio
async def test_automatic_shortlist_is_retired() -> None:
    assert await suggest_featured_lalafo.run() == 0


@pytest.mark.asyncio
async def test_legacy_polling_is_retired() -> None:
    assert await process_featured_review_updates.run() == 0

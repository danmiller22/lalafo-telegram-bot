import pytest
from sqlalchemy import select, func
from app.models import PaymentHistory
from scripts.grant_lifetime_by_username_once import grant_user
from tests.helpers import make_ad


@pytest.mark.asyncio
async def test_complimentary_lifetime_without_checkout_is_idempotent_and_free(repositories, service):
    apartments, _, sessions = repositories
    assert await grant_user(sessions, 123, admin_id=999, username="recipient") == "granted"
    assert await grant_user(sessions, 123, admin_id=999, username="recipient") == "already_active"
    apartment = await apartments.upsert_discovered(make_ad())
    result = await service.contact_status(123, apartment.id)
    assert result.status == "approved" and result.access_expires_at is None
    async with sessions() as session:
        rows = list((await session.scalars(select(PaymentHistory))).all())
    assert len(rows) == 1 and rows[0].amount == 0

from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from app.availability import AvailabilityService
from app.config import Settings
from app.models import TermsConsent
from app.payments.service import PaymentService
from app.terms import TERMS_VERSION, TermsConsentRepository
from scripts.publish_inventory import _valid
from tests.helpers import make_ad


@pytest.mark.parametrize(
    ("price", "accepted"),
    [(9_999, False), (10_000, True), (50_000, True), (50_001, False)],
)
def test_release_price_boundaries(price: int, accepted: bool) -> None:
    result, _ = _valid(
        make_ad(price=price, rooms="1", photo_urls=["https://img.example/1.jpg"]),
        Settings(_env_file=None),
    )
    assert result is accepted


@pytest.mark.asyncio
async def test_terms_must_be_reaccepted_after_version_change(repositories) -> None:
    _, _, sessions = repositories
    consents = TermsConsentRepository(sessions)
    assert not await consents.accepted(123)
    await consents.accept(123)
    assert await consents.accepted(123)
    async with sessions.begin() as session:
        await session.execute(
            update(TermsConsent)
            .where(TermsConsent.telegram_user_id == 123)
            .values(version="old-version")
        )
    assert TERMS_VERSION != "old-version"
    assert not await consents.accepted(123)


@pytest.mark.asyncio
async def test_confirmed_source_removal_blocks_payment(repositories) -> None:
    apartments, payments, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=70001))
    await apartments.set_availability(
        apartment.id,
        status="unavailable",
        checked_at=datetime.now(timezone.utc),
        reason="source_removed",
    )
    service = PaymentService(apartments, payments, admin_user_id=1)
    assert (await service.contact_status(99, apartment.id)).status == "unavailable"
    with pytest.raises(LookupError):
        await service.begin_payment(
            user_id=99,
            apartment_id=apartment.id,
            username=None,
            first_name="Test",
            plan="week",
        )


@pytest.mark.asyncio
async def test_availability_result_is_cached_for_five_minutes(repositories, monkeypatch) -> None:
    apartments, _, _ = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=70002))
    service = AvailabilityService(apartments, Settings(_env_file=None))

    calls = 0

    async def active(_url: str):
        nonlocal calls
        calls += 1
        return "active", "source_available"

    monkeypatch.setattr(service, "_check_lalafo", active)
    first = await service.check(apartment.id)
    second = await service.check(apartment.id)
    assert first.status == second.status == "active"
    assert second.cached is True
    assert calls == 1

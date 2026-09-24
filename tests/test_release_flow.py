from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.availability import AvailabilityService
from app.bot.handlers import availability_handler
from app.config import Settings
from app.models import TermsConsent
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.terms import TERMS_VERSION, TermsConsentRepository
from scripts.publish_inventory import _valid
from tests.helpers import make_ad


@pytest.mark.parametrize(
    ("price", "accepted"),
    [(17_999, False), (18_000, True), (40_000, True), (40_001, False)],
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
async def test_availability_result_is_cached_for_twelve_hours(repositories, monkeypatch) -> None:
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
    assert "Последняя проверка:" in second.message


@pytest.mark.asyncio
async def test_apartment_expires_two_days_after_publication(repositories) -> None:
    apartments, _, sessions = repositories
    apartment = await apartments.upsert_discovered(make_ad(lalafo_id=70004))
    async with sessions.begin() as session:
        await session.execute(
            update(type(apartment))
            .where(type(apartment).id == apartment.id)
            .values(published_at=datetime.now(timezone.utc) - timedelta(days=2, minutes=1))
        )

    result = await AvailabilityService(
        apartments, Settings(_env_file=None)
    ).check(apartment.id)

    assert result.status == "unavailable"
    assert result.reason == "listing_age_limit"
    assert (await apartments.get(apartment.id)).active is False


@pytest.mark.asyncio
async def test_public_availability_button_works_across_cloud_secrets() -> None:
    worker_signer = TokenSigner("worker-secret-long-enough")
    bot_signer = TokenSigner("bot-secret-long-enough-value")
    result = SimpleNamespace(message="Объявление доступно на источнике.")
    availability = SimpleNamespace(check=AsyncMock(return_value=result))
    callback = SimpleNamespace(
        data=f"availability:{worker_signer.sign_id('availability', 70003)}",
        answer=AsyncMock(),
    )

    await availability_handler(callback, bot_signer, availability)

    availability.check.assert_awaited_once_with(70003)
    callback.answer.assert_awaited_once_with(result.message, show_alert=True)

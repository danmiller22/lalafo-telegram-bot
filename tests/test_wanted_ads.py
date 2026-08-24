import pytest

from app.security import TokenSigner
from app.wanted.formatting import format_wanted_ad, format_wanted_public
from app.wanted.keyboards import wanted_admin_keyboard, wanted_payment_keyboard
from app.wanted.repository import WantedAdRepository


async def make_wanted(repo: WantedAdRepository, *, user_id: int = 100):
    return await repo.create(
        user_id=user_id,
        username="tenant",
        first_name="Tenant",
        rooms="2",
        district="7 мкр",
        budget=35_000,
        move_in="с 1 сентября",
        tenants="семейная пара",
        notes="с мебелью, можно с котом",
        contact="@tenant",
    )


@pytest.mark.asyncio
async def test_wanted_ad_payment_and_publication_lifecycle(repositories):
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await make_wanted(repo)

    assert ad.status == "awaiting_payment"
    assert (await repo.submit_payment(ad.id, 999))[0] == "missing"
    outcome, submitted = await repo.submit_payment(ad.id, 100)
    assert outcome == "created"
    assert submitted is not None and submitted.status == "pending"
    assert (await repo.submit_payment(ad.id, 100))[0] == "pending"

    assert await repo.claim_admin_notification(ad.id) is True
    assert await repo.claim_admin_notification(ad.id) is False
    await repo.release_admin_notification(ad.id)
    assert await repo.claim_admin_notification(ad.id) is True
    assert await repo.finish_admin_notification(ad.id, 500) is True

    assert await repo.begin_decision(ad.id, approve=True, admin_id=999) == "publishing"
    assert await repo.begin_decision(ad.id, approve=True, admin_id=999) == "already_publishing"
    await repo.mark_published(ad.id, 777)
    published = await repo.get(ad.id)
    assert published is not None
    assert published.status == "published"
    assert published.telegram_message_id == 777
    assert (await repo.submit_payment(ad.id, 100))[0] == "published"


@pytest.mark.asyncio
async def test_rejected_wanted_ad_can_be_resubmitted(repositories):
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await make_wanted(repo, user_id=200)
    assert (await repo.submit_payment(ad.id, 200))[0] == "created"
    assert await repo.begin_decision(ad.id, approve=False, admin_id=999) == "rejected"
    assert (await repo.get(ad.id)).status == "rejected"
    assert (await repo.submit_payment(ad.id, 200))[0] == "created"
    assert (await repo.get(ad.id)).status == "pending"


@pytest.mark.asyncio
async def test_wanted_ad_format_and_owner_listing(repositories):
    _, _, sessions = repositories
    repo = WantedAdRepository(sessions)
    ad = await make_wanted(repo)
    await make_wanted(repo, user_id=200)

    text = format_wanted_ad(ad)
    assert "2 комнаты" in text
    assert "7 мкр" in text
    assert "35 000 сом" in text
    assert "семейная пара" in text
    assert "@tenant" in text
    assert format_wanted_public(ad).startswith("🔎 Ищу")
    assert "📞 Контакты: @tenant" in format_wanted_public(ad)
    assert [row.id for row in await repo.owned(100)] == [ad.id]


def test_wanted_ad_payment_and_admin_callbacks_are_signed_and_short():
    signer = TokenSigner("a-very-long-test-secret")
    payment = wanted_payment_keyboard(
        123,
        signer=signer,
        payment_url="https://qr.finik.kg/payment",
        support_url="https://t.me/support",
    )
    admin = wanted_admin_keyboard(123, signer=signer)

    paid_data = payment.inline_keyboard[1][0].callback_data
    approve_data = admin.inline_keyboard[0][0].callback_data
    reject_data = admin.inline_keyboard[0][1].callback_data
    for callback_data in (paid_data, approve_data, reject_data):
        assert callback_data is not None
        assert len(callback_data.encode()) <= 64
    assert payment.inline_keyboard[0][0].text == "💳 Оплатить 100 сом"
    assert payment.inline_keyboard[0][0].url == "https://qr.finik.kg/payment"

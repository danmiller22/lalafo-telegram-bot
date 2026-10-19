"""Grant the explicitly authorized weekly access request 1797 exactly once."""
from __future__ import annotations

import asyncio

from aiogram import Bot

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService
from app.telegram.private_delivery import send_private_contact


REQUEST_ID = 1797
EXPECTED_USER_ID = 7318611306


async def main() -> None:
    settings = get_settings()
    if not settings.admin_user_id:
        raise RuntimeError("Payment administrator is not configured")
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    try:
        await init_db(engine)
        payments = PaymentRepository(sessions)
        apartments = ApartmentRepository(sessions)
        service = PaymentService(
            apartments, payments, admin_user_id=settings.admin_user_id,
        )
        request = await service.get_request(REQUEST_ID)
        if request is None:
            raise RuntimeError("Authorized payment request is missing")
        if request.telegram_user_id != EXPECTED_USER_ID:
            raise RuntimeError("Authorized payment request belongs to another user")
        outcome = await service.decide(
            REQUEST_ID, approve=True, actor_id=settings.admin_user_id,
        )
        if outcome not in {"approved", "already_approved"}:
            raise RuntimeError(f"Access grant failed: {outcome}")
        current = await service.get_request(REQUEST_ID)
        if current is None or current.status != "approved":
            raise RuntimeError("Fresh read did not confirm approved access")
        if outcome == "approved":
            await bot.send_message(
                EXPECTED_USER_ID,
                "Оплата подтверждена. Доступ к контактам открыт.",
            )
            if current.apartment is not None:
                await send_private_contact(
                    bot,
                    user_id=EXPECTED_USER_ID,
                    apartment=current.apartment,
                    support_url=settings.support_bot_url,
                    max_photos=settings.max_photos_per_apartment,
                )
        print(
            f"PAYMENT_ACCESS_CONFIRMED request_id={REQUEST_ID} "
            f"user_id={EXPECTED_USER_ID} outcome={outcome}"
        )
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

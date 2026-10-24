"""Grant the explicitly authorized Telegram user one week of contact access."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import func, select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.models import Apartment, PaymentHistory, PaymentRequest, SupportTicket, WantedAd
from app.payment_plans import WEEK_PLAN


TARGET_USERNAME = "juliette0202sss"


async def main() -> None:
    settings = get_settings()
    if not settings.admin_user_id:
        raise RuntimeError("Payment administrator is not configured")
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(settings.require_bot_token())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    try:
        await init_db(engine)
        async with sessions.begin() as session:
            identities: set[int] = set()
            first_names: dict[int, str | None] = {}
            for model in (PaymentRequest, PaymentHistory, SupportTicket, WantedAd):
                rows = await session.execute(
                    select(model.telegram_user_id, model.first_name).where(
                        func.lower(model.username) == TARGET_USERNAME
                    )
                )
                for user_id, first_name in rows:
                    identities.add(int(user_id))
                    if first_name:
                        first_names[int(user_id)] = first_name
            if not identities:
                raise RuntimeError("Telegram user has not interacted with the bot")
            if len(identities) != 1:
                raise RuntimeError("Username resolves to multiple Telegram users")
            user_id = identities.pop()

            active = await session.scalar(
                select(PaymentRequest)
                .where(
                    PaymentRequest.telegram_user_id == user_id,
                    PaymentRequest.status == "approved",
                    PaymentRequest.plan == WEEK_PLAN,
                    PaymentRequest.access_expires_at > now,
                )
                .order_by(PaymentRequest.access_expires_at.desc())
                .limit(1)
            )
            if active is not None:
                granted = False
                expires_at = active.access_expires_at
            else:
                request = await session.scalar(
                    select(PaymentRequest)
                    .where(PaymentRequest.telegram_user_id == user_id)
                    .order_by(PaymentRequest.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
                if request is None:
                    apartment_id = await session.scalar(
                        select(Apartment.id)
                        .where(
                            Apartment.active.is_(True),
                            Apartment.phone != "",
                            Apartment.publication_status == "published",
                        )
                        .order_by(Apartment.published_at.desc())
                        .limit(1)
                    )
                    if apartment_id is None:
                        raise RuntimeError("No contact card is available for the access grant")
                    request = PaymentRequest(
                        telegram_user_id=user_id,
                        apartment_id=apartment_id,
                        username=TARGET_USERNAME,
                        first_name=first_names.get(user_id),
                    )
                    session.add(request)
                request.username = TARGET_USERNAME
                request.plan = WEEK_PLAN
                request.status = "approved"
                request.approved_at = now
                request.approved_by = settings.admin_user_id
                request.access_expires_at = expires_at
                request.rejected_at = None
                request.rejected_by = None
                granted = True

        notification = "not_needed"
        if granted:
            try:
                await bot.send_message(
                    user_id,
                    "✅ Доступ к контактам квартир открыт на 7 дней.",
                )
                notification = "sent"
            except Exception as exc:
                notification = f"failed:{type(exc).__name__}"
        print(
            "WEEK_ACCESS_CONFIRMED "
            f"username=@{TARGET_USERNAME} user_id={user_id} "
            f"granted={str(granted).lower()} expires_at={expires_at.isoformat()} "
            f"notification={notification}"
        )
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

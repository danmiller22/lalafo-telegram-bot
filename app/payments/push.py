"""Send an optional iPhone notification for a verified XPAY payment."""

import logging

import httpx
from sqlalchemy import select

from app.models import XPayAttempt
from app.payment_plans import plan_label

logger = logging.getLogger(__name__)


async def send_xpay_push(sessions, settings, checkout_token: str) -> None:
    try:
        async with sessions() as session:
            charge = (await session.execute(
                select(XPayAttempt.expected_amount, XPayAttempt.checkout_plan)
                .where(XPayAttempt.token == checkout_token, XPayAttempt.status == "paid")
            )).one_or_none()
        if charge is None:
            return
        amount, plan = charge
        description = plan_label(plan) if plan else "Оплата Rentify"
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": settings.pushover_app_token,
                    "user": settings.pushover_user_key,
                    "title": f"XPAY: получено {amount} сом",
                    "message": description,
                },
            )
            response.raise_for_status()
    except Exception as exc:
        logger.warning("XPAY push was not delivered: %s", type(exc).__name__)

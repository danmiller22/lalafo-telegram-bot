"""Retry pending administrator notifications; this worker never issues access."""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.payments.review import notify_payment_admin
from app.listing_lifecycle import listing_expired
from app.payment_plans import WEEK_PLAN, plan_label, plan_price
from app.telegram.keyboards import payment_reminder_keyboard

logger = logging.getLogger(__name__)


async def process_payment_reviews(runtime) -> int:
    payments = runtime.workflow_data["payments"]
    settings = runtime.workflow_data["settings"]
    signer = runtime.workflow_data["signer"]
    sent = 0
    for request in await payments.pending_notifications():
        try:
            sent += bool(await notify_payment_admin(runtime.bot, payments, settings, signer, request))
        except Exception:
            logger.exception("Could not notify administrator about payment %s", request.id)
    return sent


async def process_checkout_recoveries(runtime) -> int:
    """Send one useful reminder after a customer leaves Finik without claiming."""
    payments = runtime.workflow_data["payments"]
    settings = runtime.workflow_data["settings"]
    signer = runtime.workflow_data["signer"]
    sent = 0
    for request in await payments.claim_payment_reminders():
        apartment = request.apartment
        if (
            apartment is None
            or not apartment.phone
            or apartment.availability_status == "unavailable"
            or listing_expired(apartment)
        ):
            await payments.finish_payment_reminder(request.id, include_siblings=False)
            continue
        try:
            from app.web import _finik_checkout_url

            checkout_url = await _finik_checkout_url(
                settings,
                payments,
                request,
                apartment_id=request.apartment_id,
                plan=request.plan,
            )
        except Exception:
            await payments.release_payment_reminder(request.id)
            logger.exception(
                "Could not prepare checkout recovery for request %s", request.id
            )
            continue
        price = plan_price(request.plan)
        markup = payment_reminder_keyboard(
            request.apartment_id,
            signer=signer,
            payment_url=checkout_url,
            support_url=settings.support_bot_url,
            price=price,
        )
        period = "в неделю" if request.plan == WEEK_PLAN else plan_label(request.plan).lower()
        text = (
            "Вы открывали оплату контактов квартиры.\n\n"
            f"Доступ к контактам собственников — {price} сом {period}.\n\n"
            "Если уже оплатили, нажмите «Я оплатил(а)» — администратор "
            "проверит поступление."
        )
        try:
            await runtime.bot.send_message(
                request.telegram_user_id, text, reply_markup=markup
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            # A customer who blocked the bot must not be retried indefinitely.
            await payments.finish_payment_reminder(request.id)
        except (Exception, asyncio.CancelledError):
            await payments.release_payment_reminder(request.id)
            raise
        else:
            await payments.finish_payment_reminder(request.id)
            matching = runtime.workflow_data.get("matching")
            if matching is not None:
                try:
                    profile = await matching.profile(request.telegram_user_id)
                    await matching.event(
                        request.telegram_user_id,
                        "payment_reminder_sent",
                        source=(profile.source if profile is not None else "telegram"),
                        apartment_id=request.apartment_id,
                    )
                except Exception:
                    logger.exception(
                        "Could not record checkout recovery metric for request %s",
                        request.id,
                    )
            sent += 1
    return sent


async def run_payment_review_worker(runtime) -> None:
    while True:
        try:
            await process_payment_reviews(runtime)
        except Exception:
            logger.exception("Payment review notification iteration failed")
        try:
            await process_checkout_recoveries(runtime)
        except Exception:
            logger.exception("Checkout recovery iteration failed")
        await asyncio.sleep(30)

"""Retry pending administrator notifications; this worker never issues access."""
from __future__ import annotations

import asyncio
import logging

from app.payments.review import notify_payment_admin

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


async def run_payment_review_worker(runtime) -> None:
    while True:
        try:
            await process_payment_reviews(runtime)
        except Exception:
            logger.exception("Payment review notification iteration failed")
        await asyncio.sleep(30)

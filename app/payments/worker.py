"""Durable customer-claim access grants; no provider confirmation is required."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.telegram.private_delivery import send_private_contact

logger = logging.getLogger(__name__)


async def _deliver_grant(runtime, request) -> None:
    settings = runtime.workflow_data["settings"]
    matching = runtime.workflow_data.get("matching")
    if matching is not None:
        try:
            profile = await matching.profile(request.telegram_user_id)
            await matching.event(
                request.telegram_user_id, "access_granted",
                source=profile.source if profile is not None else "telegram",
                apartment_id=request.apartment_id,
            )
        except Exception:
            logger.exception("Could not record issued access in funnel")
    try:
        await runtime.bot.send_message(request.telegram_user_id, "Доступ к контактам открыт.")
        if request.apartment:
            await send_private_contact(
                runtime.bot, user_id=request.telegram_user_id,
                apartment=request.apartment, support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment,
            )
    except Exception:
        # The Mini App can read the committed access even if Telegram fails.
        logger.exception("Could not deliver granted access for request %s", request.id)


async def process_payment_claims(runtime, *, now: datetime | None = None,
                                 delivery_tasks: set | None = None,
                                 delivery_limit: asyncio.Semaphore | None = None) -> int:
    grants = await runtime.workflow_data["payments"].approve_claims_due(now=now)

    async def deliver(request):
        if delivery_limit is None:
            await _deliver_grant(runtime, request)
        else:
            async with delivery_limit:
                await _deliver_grant(runtime, request)

    for request in grants:
        if delivery_tasks is None:
            await deliver(request)
        else:
            task = asyncio.create_task(deliver(request), name=f"payment-access-delivery-{request.id}")
            delivery_tasks.add(task)
            task.add_done_callback(delivery_tasks.discard)
    return len(grants)


async def run_payment_claim_worker(runtime) -> None:
    deliveries = set()
    limit = asyncio.Semaphore(4)
    try:
        while True:
            try:
                # Slow Telegram uploads must not delay the next customer's grant.
                await process_payment_claims(runtime, delivery_tasks=deliveries, delivery_limit=limit)
            except Exception:
                logger.exception("Payment claim worker iteration failed")
            await asyncio.sleep(2)
    finally:
        for task in deliveries:
            task.cancel()
        await asyncio.gather(*deliveries, return_exceptions=True)

"""Print aggregate payment-funnel health without customer identifiers."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func, select

from app.database import create_engine_and_session
from app.models import CustomerFunnelEvent, PaymentHistory, PaymentRequest


async def _period(session, *, start: datetime, end: datetime) -> dict[str, object]:
    event_rows = await session.execute(
        select(
            CustomerFunnelEvent.event_name,
            func.count(CustomerFunnelEvent.id),
            func.count(distinct(CustomerFunnelEvent.telegram_user_id)),
        )
        .where(CustomerFunnelEvent.created_at >= start, CustomerFunnelEvent.created_at < end)
        .group_by(CustomerFunnelEvent.event_name)
    )
    events = {
        str(name): {"events": int(count), "users": int(users)}
        for name, count, users in event_rows
    }
    request_rows = await session.execute(
        select(PaymentRequest.status, func.count(PaymentRequest.id))
        .where(PaymentRequest.created_at >= start, PaymentRequest.created_at < end)
        .group_by(PaymentRequest.status)
    )
    provider_rows = await session.execute(
        select(PaymentRequest.provider_status, func.count(PaymentRequest.id))
        .where(
            PaymentRequest.created_at >= start,
            PaymentRequest.created_at < end,
            PaymentRequest.provider_status.is_not(None),
        )
        .group_by(PaymentRequest.provider_status)
    )
    paid_count, paid_som = (
        await session.execute(
            select(func.count(PaymentHistory.id), func.coalesce(func.sum(PaymentHistory.amount), 0))
            .where(PaymentHistory.paid_at >= start, PaymentHistory.paid_at < end)
        )
    ).one()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "events": events,
        "requests": {str(status): int(count) for status, count in request_rows},
        "provider": {str(status): int(count) for status, count in provider_rows},
        "paid": {"count": int(paid_count), "som": int(paid_som)},
    }


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine, sessions = create_engine_and_session(database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as session:
            result = {
                "generated_at": now.isoformat(),
                "last_24h": await _period(session, start=now - timedelta(days=1), end=now),
                "last_7d": await _period(session, start=now - timedelta(days=7), end=now),
                "previous_7d": await _period(
                    session, start=now - timedelta(days=14), end=now - timedelta(days=7)
                ),
            }
        print("PAYMENT_FUNNEL_AUDIT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

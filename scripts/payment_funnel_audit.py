"""Print aggregate payment-funnel health without customer identifiers."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, select

from app.database import create_engine_and_session
from app.models import Apartment, CustomerFunnelEvent, PaymentHistory, PaymentRequest
from app.promotions import publications_in_window, publication_counts_in_window


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
    paid_count, paid_users, paid_som = (
        await session.execute(
            select(
                func.count(PaymentHistory.id),
                func.count(distinct(PaymentHistory.telegram_user_id)),
                func.coalesce(func.sum(PaymentHistory.amount), 0),
            )
            .where(PaymentHistory.paid_at >= start, PaymentHistory.paid_at < end)
        )
    ).one()
    posts = await publications_in_window(session, start, end)
    prior = {row.apartment_id for row in await publications_in_window(
        session, start - timedelta(days=30), start)}
    fresh = {row.apartment_id for row in posts} - prior
    delivery = await publication_counts_in_window(session, start, end)
    opens = (await session.execute(
        select(CustomerFunnelEvent.apartment_id, Apartment.availability_status,
               func.count(CustomerFunnelEvent.id))
        .outerjoin(Apartment, Apartment.id == CustomerFunnelEvent.apartment_id)
        .where(CustomerFunnelEvent.created_at >= start, CustomerFunnelEvent.created_at < end,
               CustomerFunnelEvent.event_name == "card_opened")
        .group_by(CustomerFunnelEvent.apartment_id, Apartment.availability_status)
    )).all()
    if end - start < timedelta(days=2):
        print("ALIGNED_PUBLICATION=" + json.dumps({
            "start": start.isoformat(), "end": end.isoformat(),
            "posts": len(posts), "fresh_30d": len(fresh),
            "repeat_30d": len(posts) - len(fresh),
            "external_repeats": int(delivery.total - delivery.ordinary),
            "opens_on_fresh": sum(count for aid, _, count in opens if aid in fresh),
            "opens_on_unavailable_now": sum(count for _, state, count in opens if state == "unavailable"),
        }), flush=True)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "events": events,
        "requests": {str(status): int(count) for status, count in request_rows},
        "provider": {str(status): int(count) for status, count in provider_rows},
        "paid": {
            "count": int(paid_count),
            "users": int(paid_users),
            "som": int(paid_som),
        },
    }


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine, sessions = create_engine_and_session(database_url)
    now = datetime.now(timezone.utc)
    timezone_name = os.environ.get("PAYMENT_AUDIT_TIMEZONE", "Asia/Bishkek")
    local_timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(local_timezone)
    today_start = local_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    yesterday_start = today_start - timedelta(days=1)
    try:
        async with sessions() as session:
            result = {
                "generated_at": now.isoformat(),
                "timezone": timezone_name,
                "today": await _period(
                    session, start=today_start, end=now
                ),
                "yesterday": await _period(
                    session,
                    start=yesterday_start,
                    end=today_start,
                ),
                "yesterday_same_hours": await _period(session, start=yesterday_start, end=now - timedelta(days=1)),
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

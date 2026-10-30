"""One-off read-only aggregate audit; emits no customer identifiers."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, select, text

from app.database import create_engine_and_session
from app.models import CustomerFunnelEvent, PaymentHistory, PaymentRequest
from tools.revenue_diagnostics import _period_summary, _funnel_sources


async def main():
    now = datetime.now(timezone.utc)
    zone = ZoneInfo("Asia/Bishkek")
    local = now.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    evening = midnight + timedelta(hours=18)
    if evening > local:
        evening -= timedelta(days=1)
    start = evening.astimezone(timezone.utc)
    elapsed = now - start
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            if engine.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION READ ONLY"))
            result = {
                "generated_at_utc": now.isoformat(),
                "generated_at_chicago": now.astimezone(ZoneInfo("America/Chicago")).isoformat(),
                "audited_evening_bishkek": evening.isoformat(),
                "after_18": await _period_summary(session, start, now),
                "before_18_same_duration": await _period_summary(session, start-elapsed, start),
                "after_18_sources": await _funnel_sources(session, start, now),
            }
            result["previous_evenings_same_duration"] = [
                await _period_summary(session, start-timedelta(days=day), now-timedelta(days=day))
                for day in range(1,8)
            ]
            hours = []
            cursor = (start-timedelta(hours=6)).astimezone(zone)
            while cursor.astimezone(timezone.utc) < now:
                end = min(now, (cursor+timedelta(hours=1)).astimezone(timezone.utc))
                row = await _period_summary(session, cursor.astimezone(timezone.utc), end)
                row["bishkek_hour"] = cursor.isoformat()
                hours.append(row)
                cursor += timedelta(hours=1)
            result["hourly"] = hours
            latest = await session.scalar(select(func.max(PaymentHistory.paid_at)))
            result["latest_recorded_payment_at"] = latest.isoformat() if latest else None
            last_claim = await session.scalar(select(func.max(PaymentRequest.payment_claimed_at)))
            result["latest_claim_at"] = last_claim.isoformat() if last_claim else None
            pending = (await session.execute(select(
                func.count(), func.min(PaymentRequest.payment_claimed_at),
                func.count().filter(PaymentRequest.admin_message_id.is_(None)),
                func.count().filter(PaymentRequest.payment_claimed_at >= start),
            ).where(PaymentRequest.status == "pending", PaymentRequest.payment_claimed_at.is_not(None)))).one()
            result["review_backlog"] = {"claimed_pending": pending[0],
                "oldest_claim_at": pending[1].isoformat() if pending[1] else None,
                "missing_admin_notification": pending[2], "claimed_after_18": pending[3]}
            opened = select(distinct(CustomerFunnelEvent.telegram_user_id)).where(
                CustomerFunnelEvent.event_name == "card_opened",
                CustomerFunnelEvent.created_at >= start, CustomerFunnelEvent.created_at < now,
            )
            already_paid = await session.scalar(select(func.count(distinct(PaymentHistory.telegram_user_id))).where(
                PaymentHistory.telegram_user_id.in_(opened), PaymentHistory.paid_at < start,
                PaymentHistory.access_expires_at > start,
            ))
            result["after_18_visitors_with_existing_paid_access"] = already_paid
            claims = (await session.execute(select(PaymentRequest.status, func.count()).where(
                PaymentRequest.payment_claimed_at >= start, PaymentRequest.payment_claimed_at < now,
            ).group_by(PaymentRequest.status))).all()
            result["claims_after_18_current_status"] = dict(claims)
            result["last_6h"] = await _period_summary(session, now-timedelta(hours=6), now)
            result["previous_6h"] = await _period_summary(session, now-timedelta(hours=12), now-timedelta(hours=6))
            result["last_24h"] = await _period_summary(session, now-timedelta(hours=24), now)
            result["previous_24h"] = await _period_summary(session, now-timedelta(hours=48), now-timedelta(hours=24))
            result["read_only"] = True
            print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

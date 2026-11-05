"""Grant lifetime access to explicit bot users and notify them once."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select

from app.database import create_engine_and_session
from app.models import PaymentHistory, PaymentRequest
from app.payment_plans import LIFETIME_PLAN
from app.payments.repository import PaymentRepository


MESSAGE = (
    "✅ Доступ уже выдан. Теперь вы можете открыть Mini App и получить номера квартир."
)


async def main() -> None:
    requested = {
        value.strip().lstrip("@").casefold()
        for value in os.environ["LIFETIME_GRANT_USERNAMES"].split(",")
        if value.strip()
    }
    admin_id = int(os.environ["ADMIN_USER_ID"])
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    resolved: dict[str, set[int]] = {name: set() for name in requested}
    async with sessions() as session:
        rows = (
            await session.execute(
                select(PaymentRequest.username, PaymentRequest.telegram_user_id)
                .where(func.lower(PaymentRequest.username).in_(requested))
            )
        ).all()
    for username, user_id in rows:
        if username:
            resolved.setdefault(username.casefold(), set()).add(int(user_id))
    targets = [next(iter(ids)) for ids in resolved.values() if len(ids) == 1]
    granted = 0
    already_active = 0
    now = datetime.now(timezone.utc)
    try:
        for user_id in targets:
            async with sessions.begin() as session:
                existing = await session.scalar(
                    select(PaymentRequest.id).where(
                        PaymentRequest.telegram_user_id == user_id,
                        PaymentRequest.status == "approved",
                        PaymentRequest.plan == LIFETIME_PLAN,
                        PaymentRequest.access_expires_at.is_(None),
                    ).limit(1)
                )
                if existing is None:
                    existing = await session.scalar(
                        select(PaymentHistory.id).where(
                            PaymentHistory.telegram_user_id == user_id,
                            PaymentHistory.plan == LIFETIME_PLAN,
                            PaymentHistory.access_expires_at.is_(None),
                        ).limit(1)
                    )
                if existing is not None:
                    already_active += 1
                    continue
                request = await session.scalar(
                    select(PaymentRequest)
                    .where(PaymentRequest.telegram_user_id == user_id)
                    .order_by(PaymentRequest.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
                if request is None:
                    continue
                request.plan = LIFETIME_PLAN
                PaymentRepository._grant_access(
                    session, request, now=now, approved_by=admin_id
                )
                await PaymentRepository._close_sibling_requests(
                    session, request, now=now, approved_by=admin_id
                )
                granted += 1
    finally:
        await engine.dispose()

    receipts: list[dict[str, object]] = []
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    async with httpx.AsyncClient(timeout=20) as client:
        for chat_id in targets:
            payload = (
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": MESSAGE},
                )
            ).json()
            receipts.append({
                "sent": bool(payload.get("ok")),
                "error": None if payload.get("ok") else payload.get("description"),
            })
    sent = sum(bool(row["sent"]) for row in receipts)
    summary = {
        "status": "success" if len(targets) == len(requested) and sent == len(targets) else "partial",
        "requested": len(requested),
        "resolved": len(targets),
        "granted": granted,
        "already_active": already_active,
        "notified": sent,
        "missing_or_ambiguous": len(requested) - len(targets),
        "receipts": receipts,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if summary["status"] != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())

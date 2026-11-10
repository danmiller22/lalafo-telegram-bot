"""Resolve explicit Telegram usernames and create idempotent complimentary grants."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database import create_engine_and_session
from app.models import PaymentHistory, PaymentRequest, SupportTicket, WantedAd
from app.payment_plans import LIFETIME_PLAN


async def grant_user(sessions, user_id: int, *, admin_id: int, username: str) -> str:
    if admin_id <= 0 or user_id <= 0:
        raise ValueError("An explicit administrator and user are required")
    try:
        async with sessions.begin() as session:
            existing = await session.scalar(select(PaymentHistory.id).where(
                PaymentHistory.telegram_user_id == user_id,
                PaymentHistory.plan == LIFETIME_PLAN,
                PaymentHistory.access_expires_at.is_(None),
            ).limit(1))
            active_request = await session.scalar(select(PaymentRequest.id).where(
                PaymentRequest.telegram_user_id == user_id,
                PaymentRequest.plan == LIFETIME_PLAN,
                PaymentRequest.status == "approved",
                PaymentRequest.access_expires_at.is_(None),
            ).limit(1))
            if existing is not None or active_request is not None:
                return "already_active"
            session.add(PaymentHistory(
                payment_request_id=0, telegram_user_id=user_id, username=username,
                apartment_id=0, plan=LIFETIME_PLAN, amount=0,
                provider_payment_id=f"admin-lifetime-{user_id}",
                paid_at=datetime.now(timezone.utc), access_expires_at=None,
            ))
        return "granted"
    except IntegrityError:
        # The unique grant ID serializes concurrent invocations for this user.
        return "already_active"


async def main():
    requested = {v.strip().lstrip("@").casefold() for v in os.environ["LIFETIME_GRANT_USERNAMES"].split(",") if v.strip()}
    admin_id = int(os.environ["ADMIN_USER_ID"])
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    candidates = {admin_id}
    try:
        async with sessions() as session:
            for model in (PaymentRequest, PaymentHistory, SupportTicket, WantedAd):
                candidates.update((await session.scalars(select(model.telegram_user_id).where(
                    func.lower(model.username).in_(requested)))).all())
        resolved = {name: set() for name in requested}
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        async with httpx.AsyncClient(timeout=20) as client:
            for user_id in candidates:
                try:
                    result = (await client.post(f"https://api.telegram.org/bot{token}/getChat", json={"chat_id":int(user_id)})).json()
                except Exception:
                    continue
                chat = result.get("result", {}) if result.get("ok") else {}
                name = str(chat.get("username") or "").casefold()
                if name in resolved and chat.get("type") == "private" and int(chat.get("id",0)) == int(user_id):
                    resolved[name].add(int(user_id))
        outcomes = []
        for name, ids in resolved.items():
            if len(ids) == 1:
                outcomes.append(await grant_user(sessions, next(iter(ids)), admin_id=admin_id, username=name))
        summary = {"requested":len(requested), "resolved":len(outcomes),
            "granted":outcomes.count("granted"), "already_active":outcomes.count("already_active"),
            "missing_or_ambiguous":len(requested)-len(outcomes), "notifications_sent":0}
        print(json.dumps(summary))
        if summary["missing_or_ambiguous"]:
            raise SystemExit(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

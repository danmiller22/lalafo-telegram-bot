"""Send a payment-recovery instruction to explicit Telegram usernames."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from sqlalchemy import func, select

from app.database import create_engine_and_session
from app.models import PaymentRequest


MESSAGE = (
    "Пожалуйста, снова откройте Mini App и нажмите «Уже оплатил(а), но доступ "
    "не открылся», чтобы получить доступ."
)


async def main() -> None:
    requested = {
        value.strip().lstrip("@").casefold()
        for value in os.environ["PAYMENT_RECOVERY_USERNAMES"].split(",")
        if value.strip()
    }
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(PaymentRequest.username, PaymentRequest.telegram_user_id)
                    .where(func.lower(PaymentRequest.username).in_(requested))
                )
            ).all()
    finally:
        await engine.dispose()

    matches: dict[str, set[int]] = {username: set() for username in requested}
    for username, user_id in rows:
        if username:
            matches.setdefault(username.casefold(), set()).add(int(user_id))
    targets = [next(iter(ids)) for ids in matches.values() if len(ids) == 1]
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
        "status": "success" if sent == len(requested) else "partial",
        "requested": len(requested),
        "resolved": len(targets),
        "sent": sent,
        "missing_or_ambiguous": len(requested) - len(targets),
        "receipts": receipts,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if sent != len(requested):
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())

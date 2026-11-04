"""Send the approved refund notice to an explicit private set of Telegram users."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import httpx
from sqlalchemy import select

from app.database import create_engine_and_session
from app.models import PaymentHistory, PaymentRequest


MESSAGE = (
    "Приносим извинения за доставленные неудобства. "
    "В течение 1 часа мы вернём вам деньги обратно."
)


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    target_hashes = {
        value.strip()
        for value in os.environ["CUSTOMER_NOTICE_TARGET_HASHES"].split(",")
        if value.strip()
    }
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            user_ids = set(await session.scalars(select(PaymentHistory.telegram_user_id)))
            user_ids.update(await session.scalars(select(PaymentRequest.telegram_user_id)))
    finally:
        await engine.dispose()
    targets = sorted(
        user_id
        for user_id in user_ids
        if hashlib.sha256(str(user_id).encode()).hexdigest() in target_hashes
    )
    if len(targets) != len(target_hashes):
        print(json.dumps({"status": "target_mismatch", "requested": len(target_hashes), "resolved": len(targets)}))
        raise SystemExit(3)
    results: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for chat_id in targets:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": MESSAGE},
                )
                payload = response.json()
                results.append(
                    {
                        "sent": bool(payload.get("ok")),
                        "message_id": (payload.get("result") or {}).get("message_id"),
                        "error": None if payload.get("ok") else payload.get("description"),
                    }
                )
            except Exception as exc:
                results.append({"sent": False, "message_id": None, "error": type(exc).__name__})
    sent = sum(bool(item["sent"]) for item in results)
    print(json.dumps({"status": "success" if sent == len(targets) else "partial", "requested": len(targets), "sent": sent, "receipts": results}, ensure_ascii=False))
    if sent != len(targets):
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())

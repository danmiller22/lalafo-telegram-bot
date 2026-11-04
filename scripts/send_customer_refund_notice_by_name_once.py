"""Resolve prior payment-request users by current display-name hashes and notify once."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import unicodedata

import httpx
from sqlalchemy import select

from app.database import create_engine_and_session
from app.models import PaymentRequest
from scripts.send_customer_refund_notice_once import MESSAGE


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    target_hashes = {value for value in os.environ["CUSTOMER_NOTICE_NAME_HASHES"].split(",") if value}
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            user_ids = sorted(set(await session.scalars(select(PaymentRequest.telegram_user_id))))
    finally:
        await engine.dispose()

    matches: dict[str, list[int]] = {value: [] for value in target_hashes}
    async with httpx.AsyncClient(timeout=20) as client:
        for start in range(0, len(user_ids), 20):
            batch = user_ids[start:start + 20]
            responses = await asyncio.gather(
                *(client.post(f"https://api.telegram.org/bot{token}/getChat", json={"chat_id": user_id}) for user_id in batch),
                return_exceptions=True,
            )
            for user_id, response in zip(batch, responses):
                if isinstance(response, Exception):
                    continue
                payload = response.json()
                chat = payload.get("result") if payload.get("ok") else None
                if not isinstance(chat, dict):
                    continue
                full_name = " ".join(value for value in (chat.get("first_name"), chat.get("last_name")) if value)
                digest = hashlib.sha256(normalized(full_name).encode()).hexdigest()
                if digest in matches:
                    matches[digest].append(user_id)
            await asyncio.sleep(0.8)

        unique_targets = [ids[0] for ids in matches.values() if len(ids) == 1]
        receipts = []
        for chat_id in unique_targets:
            payload = (await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": MESSAGE},
            )).json()
            receipts.append({"sent": bool(payload.get("ok")), "message_id": (payload.get("result") or {}).get("message_id"), "error": None if payload.get("ok") else payload.get("description")})

    sent = sum(bool(item["sent"]) for item in receipts)
    summary = {"status": "success" if sent == len(target_hashes) else "partial", "requested": len(target_hashes), "unique_matches": len(unique_targets), "sent": sent, "ambiguous_or_missing": len(target_hashes) - len(unique_targets), "receipts": receipts}
    print(json.dumps(summary, ensure_ascii=False))
    if sent != len(target_hashes):
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())

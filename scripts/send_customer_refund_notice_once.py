"""Send the approved refund notice to an explicit private set of Telegram users."""

from __future__ import annotations

import asyncio
import json
import os

import httpx


MESSAGE = (
    "Приносим извинения за доставленные неудобства. "
    "В течение 1 часа мы вернём вам деньги обратно."
)


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    targets = sorted(
        {int(value) for value in os.environ["CUSTOMER_NOTICE_TARGETS_ONCE"].split(",")}
    )
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

"""Clear duplicate unfinished Finik checkouts while preserving payments and access."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict

from sqlalchemy import select

from app.database import create_engine_and_session
from app.models import PaymentRequest


async def main() -> None:
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    cleared = 0
    groups_with_duplicates = 0
    try:
        async with sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(PaymentRequest)
                        .where(
                            PaymentRequest.provider_payment_id.is_not(None),
                            PaymentRequest.status != "approved",
                            PaymentRequest.provider_status != "succeeded",
                        )
                        .order_by(
                            PaymentRequest.telegram_user_id,
                            PaymentRequest.plan,
                            PaymentRequest.created_at.desc(),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            grouped: dict[tuple[int, str], list[PaymentRequest]] = defaultdict(list)
            for row in rows:
                grouped[(row.telegram_user_id, row.plan)].append(row)
            for checkouts in grouped.values():
                if len(checkouts) < 2:
                    continue
                groups_with_duplicates += 1
                for duplicate in checkouts[1:]:
                    duplicate.provider_payment_id = None
                    duplicate.provider_payment_url = None
                    duplicate.provider_status = None
                    cleared += 1
        print(json.dumps({
            "status": "completed",
            "groups_with_duplicates": groups_with_duplicates,
            "duplicates_cleared": cleared,
        }))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

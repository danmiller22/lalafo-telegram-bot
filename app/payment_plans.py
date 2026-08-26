from __future__ import annotations

from datetime import datetime, timedelta


SINGLE_PLAN = "single"
WEEK_PLAN = "week"
SINGLE_PRICE = 200
WEEK_PRICE = 700
WEEK_DURATION = timedelta(days=7)


def plan_price(plan: str) -> int:
    return WEEK_PRICE if plan == WEEK_PLAN else SINGLE_PRICE


def plan_label(plan: str) -> str:
    return "Доступ на неделю" if plan == WEEK_PLAN else "Один номер"


def expires_at_for(plan: str, approved_at: datetime) -> datetime | None:
    return approved_at + WEEK_DURATION if plan == WEEK_PLAN else None

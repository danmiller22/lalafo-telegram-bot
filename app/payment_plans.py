from __future__ import annotations

from datetime import datetime, timedelta


WEEK_PLAN = "week"
WEEK_PRICE = 700
WEEK_DURATION = timedelta(days=7)


def plan_price(plan: str) -> int:
    if plan != WEEK_PLAN:
        raise ValueError("Unsupported payment plan")
    return WEEK_PRICE


def plan_label(plan: str) -> str:
    return "Доступ на неделю" if plan == WEEK_PLAN else "Архивный тариф"


def expires_at_for(plan: str, approved_at: datetime) -> datetime | None:
    return approved_at + WEEK_DURATION if plan == WEEK_PLAN else None

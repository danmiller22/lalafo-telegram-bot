from __future__ import annotations

from datetime import datetime, timedelta


WEEK_PLAN = "week"
MONTH_PLAN = "month"
WEEK_PRICE = 400
MONTH_PRICE = 999
WANTED_SEARCH_PRICE = 100
WEEK_DURATION = timedelta(days=7)
MONTH_DURATION = timedelta(days=30)


def plan_price(plan: str) -> int:
    prices = {WEEK_PLAN: WEEK_PRICE, MONTH_PLAN: MONTH_PRICE}
    try:
        return prices[plan]
    except KeyError as exc:
        raise ValueError("Unsupported payment plan") from exc


def plan_label(plan: str) -> str:
    labels = {WEEK_PLAN: "Базовая (7 дней)", MONTH_PLAN: "Месяц (30 дней)"}
    return labels.get(plan, "Неизвестный тариф")


def expires_at_for(plan: str, approved_at: datetime) -> datetime | None:
    durations = {WEEK_PLAN: WEEK_DURATION, MONTH_PLAN: MONTH_DURATION}
    duration = durations.get(plan)
    return approved_at + duration if duration is not None else None

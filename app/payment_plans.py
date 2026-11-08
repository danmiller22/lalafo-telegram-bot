from __future__ import annotations

from datetime import datetime, timedelta


WEEK_PLAN = "week"
MONTH_PLAN = "month"
LIFETIME_PLAN = "lifetime"
PERSONAL_PLAN = "personal"
PERSONAL_PRICE = 1499
PERSONAL_LIMIT = 20
PERSONAL_DURATION = timedelta(days=30)
LIFETIME_PRICE = 999
WEEK_PRICE = 499
MONTH_PRICE = 999
WANTED_SEARCH_PRICE = 100
WEEK_DURATION = timedelta(days=7)
MONTH_DURATION = timedelta(days=30)


def plan_price(plan: str) -> int:
    prices = {PERSONAL_PLAN: PERSONAL_PRICE, WEEK_PLAN: WEEK_PRICE, MONTH_PLAN: MONTH_PRICE, LIFETIME_PLAN: LIFETIME_PRICE}
    try:
        return prices[plan]
    except KeyError as exc:
        raise ValueError("Unsupported payment plan") from exc


def plan_label(plan: str) -> str:
    labels = {PERSONAL_PLAN: "Индивидуальный подбор: 1 месяц, до 20 новых квартир", WEEK_PLAN: "Базовая: 7 дней", MONTH_PLAN: "Премиум: 30 дней", LIFETIME_PLAN: "Доступ к контактам навсегда"}
    return labels.get(plan, "Неизвестный тариф")


def expires_at_for(plan: str, approved_at: datetime) -> datetime | None:
    durations = {PERSONAL_PLAN: PERSONAL_DURATION, WEEK_PLAN: WEEK_DURATION, MONTH_PLAN: MONTH_DURATION}
    duration = durations.get(plan)
    return approved_at + duration if duration is not None else None

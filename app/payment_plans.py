from __future__ import annotations

from datetime import datetime, timedelta


# Keep the persisted checkout identifier and existing Finik checkout identifiers compatible.
# New grants on this tariff are lifetime; old dated grants retain their dates.
WEEK_PLAN = "week"
LIFETIME_ACCESS_PLANS = (WEEK_PLAN, "lifetime")
MONTH_PLAN = "month"
LIFETIME_PLAN = "lifetime"
PERSONAL_PLAN = "personal"
PERSONAL_PRICE = 999
PERSONAL_DURATION = timedelta(days=30)
LIFETIME_PRICE = 999
WEEK_PRICE = LIFETIME_PRICE
WEEK_FINIK_QR = "https://qr.finik.kg/#00020101021232810011qr.finik.kg0114averspay-items10321e56beca6e24471583e9a24a0e09ed951202121302125204482953034175405499005908Finik-QR63047f08"
MONTH_PRICE = 999
WANTED_SEARCH_PRICE = 50
WEEK_DURATION = timedelta(days=7)
MONTH_DURATION = timedelta(days=30)


def plan_price(plan: str) -> int:
    prices = {PERSONAL_PLAN: PERSONAL_PRICE, WEEK_PLAN: WEEK_PRICE, MONTH_PLAN: MONTH_PRICE, LIFETIME_PLAN: LIFETIME_PRICE}
    try:
        return prices[plan]
    except KeyError as exc:
        raise ValueError("Unsupported payment plan") from exc


def plan_label(plan: str) -> str:
    labels = {PERSONAL_PLAN: "Под вас: персональный подбор на 30 дней", WEEK_PLAN: "Бессрочный доступ", MONTH_PLAN: "Премиум: 30 дней", LIFETIME_PLAN: "Доступ к контактам навсегда"}
    return labels.get(plan, "Неизвестный тариф")


def expires_at_for(plan: str, approved_at: datetime) -> datetime | None:
    durations = {PERSONAL_PLAN: PERSONAL_DURATION, MONTH_PLAN: MONTH_DURATION}
    duration = durations.get(plan)
    return approved_at + duration if duration is not None else None

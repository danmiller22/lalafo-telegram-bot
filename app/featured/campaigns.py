from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def price_id_for_daily_budget(payload: dict[str, Any], budget: int) -> int:
    """Resolve the price product returned for one ad; never guess an id."""
    matches: list[int] = []
    for item in _walk(payload):
        raw_id = item.get("price_id", item.get("id"))
        raw_value = item.get("price", item.get("value", item.get("amount")))
        try:
            item_id = int(raw_id)
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        # The UI analytics uses minor units while API objects may use either.
        if value in {float(budget), float(budget * 100)}:
            matches.append(item_id)
    if not matches:
        raise ValueError(f"No verified {budget} KGS daily campaign price exists")
    return matches[0]


def available_balance(payload: dict[str, Any]) -> float | None:
    for item in _walk(payload):
        for key in ("available_sum", "balance", "amount", "sum"):
            try:
                value = float(item[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value >= 0:
                return value / 100 if value > 10_000 else value
    return None


def campaign_identity(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    for item in _walk(payload):
        raw_id = item.get("campaign_id", item.get("id"))
        status = item.get("status")
        if raw_id is not None and status is not None:
            return str(raw_id), str(status)
    return None, None

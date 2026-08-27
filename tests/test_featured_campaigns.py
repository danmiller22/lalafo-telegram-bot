from __future__ import annotations

import pytest

from app.featured.campaigns import available_balance, campaign_identity, price_id_for_daily_budget


def test_resolves_exact_daily_price() -> None:
    payload = {"products": [{"prices": [{"id": 10, "value": 150}, {"id": 20, "value": 200}]}]}
    assert price_id_for_daily_budget(payload, 200) == 20


def test_refuses_to_guess_missing_price() -> None:
    with pytest.raises(ValueError):
        price_id_for_daily_budget({"products": [{"id": 10, "value": 250}]}, 200)


def test_extracts_balance_and_campaign() -> None:
    assert available_balance({"accounts": [{"balance": 800}]}) == 800
    assert campaign_identity({"campaign": {"id": 91, "status": "active"}}) == ("91", "active")

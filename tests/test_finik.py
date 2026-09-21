from __future__ import annotations

import json
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import pytest

from app.finik import (
    FinikClient,
    canonical_request,
    payment_configuration_id,
    payment_succeeded,
    sign_request,
    verify_request,
)
from app.web import PAYMENT_REVIEW_MESSAGE


def _keys() -> tuple[str, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def test_finik_signature_matches_canonical_request() -> None:
    private_pem, public_pem = _keys()
    data = canonical_request(
        method="POST",
        path="/v1/payment",
        host="api.acquiring.averspay.kg",
        headers={"x-api-timestamp": "123", "x-api-key": "test"},
        body={"PaymentId": "id", "Amount": 499, "Data": {"z": 1, "a": 2}},
    )
    assert data.decode().startswith(
        "post\n/v1/payment\nhost:api.acquiring.averspay.kg&x-api-key:test&x-api-timestamp:123\n"
    )
    # Only top-level JSON keys are sorted, matching Finik's official authorizer.
    assert data.decode().endswith(
        '{"Amount":499,"Data":{"z":1,"a":2},"PaymentId":"id"}'
    )
    signature = sign_request(data, private_pem)
    assert verify_request(data, signature, public_pem)
    assert not verify_request(data + b"x", signature, public_pem)


def test_finik_success_status_variants() -> None:
    assert payment_succeeded("success")
    assert payment_succeeded("SUCCEEDED")
    assert payment_succeeded(" succeeded ")
    assert not payment_succeeded("failed")
    assert not payment_succeeded(None)


def test_payment_configuration_id_changes_with_merchant() -> None:
    first = payment_configuration_id(
        api_url="https://api.acquiring.averspay.kg/v1/payment",
        account_id="corporate-account",
    )
    assert first == payment_configuration_id(
        api_url="HTTPS://API.ACQUIRING.AVERSPAY.KG/v1/payment",
        account_id="corporate-account",
    )
    assert first != payment_configuration_id(
        api_url="https://api.acquiring.averspay.kg/v1/payment",
        account_id="old-personal-account",
    )


def test_payment_review_message_explains_manual_confirmation() -> None:
    assert "отправлена на проверку" in PAYMENT_REVIEW_MESSAGE
    assert "После подтверждения" in PAYMENT_REVIEW_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "expected_description"),
    [(499, "Недельный тариф"), (999, "Месячный тариф")],
)
async def test_checkout_uses_neutral_tariff_description(
    amount: int, expected_description: str
) -> None:
    private_pem, _ = _keys()
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"url": "https://qr.finik.kg/payment"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FinikClient(
            api_url="https://api.acquiring.averspay.kg/v1/payment",
            api_key="test",
            account_id="corporate",
            private_key_pem=private_pem,
            client=http,
        )
        await client.create_payment(
            payment_id="payment-id",
            amount=amount,
            redirect_url="https://t.me/test_bot",
            webhook_url="https://example.test/finik/webhook",
        )

    assert captured["Data"]["description"] == expected_description
    assert isinstance(captured["Data"]["endDate"], int)
    remaining_ms = captured["Data"]["endDate"] - int(time.time() * 1000)
    assert 0 < remaining_ms <= 5 * 60 * 1000

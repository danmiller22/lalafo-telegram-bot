from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


PRODUCTION_WEBHOOK_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuF/PUmhMPPidcMxhZBPb
BSGJoSphmCI+h6ru8fG8guAlcPMVlhs+ThTjw2LHABvciwtpj51ebJ4EqhlySPyT
hqSfXI6Jp5dPGJNDguxfocohaz98wvT+WAF86DEglZ8dEsfoumojFUy5sTOBdHEu
g94B4BbrJvjmBa1YIx9Azse4HFlWhzZoYPgyQpArhokeHOHIN2QFzJqeriANO+wV
aUMta2AhRVZHbfyJ36XPhGO6A5FYQWgjzkI65cxZs5LaNFmRx6pjnhjIeVKKgF99
4OoYCzhuR9QmWkPl7tL4Kd68qa/xHLz0Psnuhm0CStWOYUu3J7ZpzRK8GoEXRcr8
tQIDAQAB
-----END PUBLIC KEY-----"""


def _compact_json(value: Mapping[str, Any]) -> str:
    # The official authorizer sorts top-level keys only. Nested order is kept.
    ordered = {key: value[key] for key in sorted(value)}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def canonical_request(
    *, method: str, path: str, host: str, headers: Mapping[str, str], body: Mapping[str, Any]
) -> bytes:
    selected = [f"host:{host}"]
    for key in sorted(key for key in headers if key.lower().startswith("x-api-")):
        selected.append(f"{key.lower()}:{headers[key]}")
    parts = [method.lower(), unquote(path), "&".join(selected), _compact_json(body)]
    return "\n".join(parts).encode("utf-8")


def sign_request(data: bytes, private_key_pem: str) -> str:
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def verify_request(data: bytes, signature: str, public_key_pem: str) -> bool:
    try:
        raw_signature = base64.b64decode(signature, validate=True)
        key = serialization.load_pem_public_key(public_key_pem.encode())
        key.verify(raw_signature, data, padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def decode_private_key(*, pem: str, encoded: str) -> str:
    if pem:
        return pem.replace("\\n", "\n")
    if encoded:
        return base64.b64decode(encoded).decode("utf-8")
    raise RuntimeError("Finik private key is not configured")


def payment_succeeded(status: object) -> bool:
    """Accept both success values used by Finik webhook examples."""
    return str(status or "").strip().casefold() in {"success", "succeeded"}


def payment_configuration_id(*, api_url: str, account_id: str) -> str:
    """Return a non-secret marker used to invalidate links from an old merchant."""
    value = f"{api_url.strip().lower()}|{account_id.strip()}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class FinikPayment:
    payment_id: str
    url: str


class FinikClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        account_id: str,
        private_key_pem: str,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.account_id = account_id
        self.private_key_pem = private_key_pem
        self.timeout = timeout
        self.client = client

    async def create_payment(
        self,
        *,
        payment_id: str,
        amount: int,
        redirect_url: str,
        webhook_url: str,
    ) -> FinikPayment:
        parsed = urlsplit(self.api_url)
        timestamp = str(int(time.time() * 1000))
        body: dict[str, Any] = {
            "Amount": amount,
            "CardType": "FINIK_QR",
            "PaymentId": payment_id,
            "RedirectUrl": redirect_url,
            "Lang": "ru",
            "Data": {
                "accountId": self.account_id,
                "name_en": "Arenda.KG",
                "webhookUrl": webhook_url,
                "description": "Доступ к номерам квартир",
            },
        }
        signing_headers = {
            "Host": parsed.netloc,
            "x-api-key": self.api_key,
            "x-api-timestamp": timestamp,
        }
        signature = sign_request(
            canonical_request(
                method="POST",
                path=parsed.path,
                host=parsed.netloc,
                headers=signing_headers,
                body=body,
            ),
            self.private_key_pem,
        )
        if self.client is not None:
            response = await self.client.post(
                self.api_url,
                headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                    "x-api-timestamp": timestamp,
                    "signature": signature,
                },
                json=body,
            )
        else:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "content-type": "application/json",
                        "x-api-key": self.api_key,
                        "x-api-timestamp": timestamp,
                        "signature": signature,
                    },
                    json=body,
                )
        location = response.headers.get("location")
        if not location and response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            location = payload.get("url") or payload.get("paymentUrl") or payload.get("redirectUrl")
        if response.status_code not in {200, 201, 302, 303} or not location:
            detail = response.text[:500]
            raise RuntimeError(f"Finik create payment failed ({response.status_code}): {detail}")
        if not location.startswith("https://"):
            raise RuntimeError("Finik returned an unsafe payment URL")
        return FinikPayment(payment_id=payment_id, url=location)

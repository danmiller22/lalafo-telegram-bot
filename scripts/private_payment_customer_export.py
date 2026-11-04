"""Export paid Telegram customers encrypted for a one-time private audit."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from app.database import create_engine_and_session
from app.models import PaymentHistory, PaymentRequest


async def main() -> None:
    public_key = serialization.load_pem_public_key(
        base64.b64decode(os.environ["AUDIT_PUBLIC_KEY_B64"])
    )
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(PaymentHistory, PaymentRequest)
                    .outerjoin(
                        PaymentRequest,
                        PaymentRequest.id == PaymentHistory.payment_request_id,
                    )
                    .order_by(PaymentHistory.paid_at)
                )
            ).all()
        payload = json.dumps(
            [
                {
                    "telegram_user_id": history.telegram_user_id,
                    "username": history.username,
                    "first_name": history.first_name,
                    "plan": history.plan,
                    "amount": history.amount,
                    "paid_at": history.paid_at.isoformat(),
                    "provider_payment_id": history.provider_payment_id,
                    "request_created_at": (
                        request.created_at.isoformat() if request else None
                    ),
                    "provider_status": request.provider_status if request else None,
                    "approved_by": request.approved_by if request else None,
                }
                for history, request in rows
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, payload, None)
        wrapped_key = public_key.encrypt(
            key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        envelope = {
            "wrapped_key": base64.b64encode(wrapped_key).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(encrypted).decode(),
        }
        Path("private-payment-customers.enc.json").write_text(
            json.dumps(envelope, separators=(",", ":")), encoding="utf-8"
        )
        encoded = base64.b64encode(json.dumps(envelope).encode()).decode()
        for index in range(0, len(encoded), 8000):
            print(
                f"PAYMENT_CUSTOMERS_ENCRYPTED_{index // 8000:03d}="
                + encoded[index : index + 8000],
                flush=True,
            )
        print(f"Encrypted {len(rows)} payment records")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

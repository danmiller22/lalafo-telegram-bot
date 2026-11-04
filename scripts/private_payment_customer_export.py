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
from app.models import PaymentHistory


async def main() -> None:
    public_key = serialization.load_pem_public_key(
        base64.b64decode(os.environ["AUDIT_PUBLIC_KEY_B64"])
    )
    engine, sessions = create_engine_and_session(os.environ["DATABASE_URL"])
    try:
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(PaymentHistory).order_by(PaymentHistory.paid_at)
                )
            ).all()
        payload = json.dumps(
            [
                {
                    "telegram_user_id": row.telegram_user_id,
                    "username": row.username,
                    "first_name": row.first_name,
                    "plan": row.plan,
                    "amount": row.amount,
                    "paid_at": row.paid_at.isoformat(),
                }
                for row in rows
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

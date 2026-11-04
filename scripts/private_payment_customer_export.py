"""Export paid Telegram customers encrypted for a one-time private audit."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageEnhance, ImageOps
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from app.database import create_engine_and_session
from app.models import PaymentHistory, PaymentRequest


def _phone_hashes_from_text(text: str, target_hashes: set[str]) -> list[str]:
    candidates: set[str] = set()
    for value in re.findall(r"(?:\+?996|0)[\d\s()\-]{8,20}", text):
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10 and digits.startswith("0"):
            digits = "996" + digits[1:]
        if len(digits) == 12 and digits.startswith("996"):
            candidates.add(digits)
    compact = re.sub(r"\D", "", text)
    candidates.update(
        compact[index : index + 12]
        for index in range(max(0, len(compact) - 11))
        if compact[index : index + 3] == "996"
    )
    return sorted(
        digest
        for digits in candidates
        if (digest := hashlib.sha256(digits.encode()).hexdigest()) in target_hashes
    )


async def _receipt_phone_matches(
    rows: list[tuple[PaymentHistory, PaymentRequest | None]],
) -> dict[int, list[str]]:
    target_hashes = {
        value.strip()
        for value in os.environ.get("AUDIT_TARGET_PHONE_HASHES", "").split(",")
        if value.strip()
    }
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not target_hashes or not token:
        return {}
    matches: dict[int, list[str]] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for history, request in rows[-160:]:
            if request is None or not request.receipt_file_id:
                continue
            metadata = (
                await client.post(
                    f"https://api.telegram.org/bot{token}/getFile",
                    json={"file_id": request.receipt_file_id},
                )
            ).json()
            path = (metadata.get("result") or {}).get("file_path")
            if not path:
                continue
            content = (
                await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
            ).content
            try:
                with Image.open(__import__("io").BytesIO(content)) as source:
                    image = ImageOps.grayscale(source)
                    image = ImageOps.autocontrast(image).resize(
                        (image.width * 2, image.height * 2)
                    )
                    variants = [image, ImageEnhance.Contrast(image).enhance(2.5)]
                    text = ""
                    with tempfile.TemporaryDirectory() as directory:
                        for index, variant in enumerate(variants):
                            image_path = Path(directory) / f"receipt-{index}.png"
                            variant.save(image_path)
                            result = subprocess.run(
                                ["tesseract", str(image_path), "stdout", "--psm", "6"],
                                capture_output=True,
                                text=True,
                                timeout=30,
                                check=False,
                            )
                            text += "\n" + result.stdout
                found = _phone_hashes_from_text(text, target_hashes)
                if found:
                    matches[history.id] = found
            except Exception:
                continue
    return matches


async def _current_telegram_profiles(user_ids: list[int]) -> dict[int, dict[str, object]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {}
    profiles: dict[int, dict[str, object]] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        async def fetch(user_id: int) -> tuple[int, dict[str, object] | None]:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/getChat",
                json={"chat_id": user_id},
            )
            payload = response.json()
            if payload.get("ok") and isinstance(payload.get("result"), dict):
                chat = payload["result"]
                return user_id, {
                    "current_first_name": chat.get("first_name"),
                    "current_last_name": chat.get("last_name"),
                    "current_username": chat.get("username"),
                }
            return user_id, None

        for start in range(0, len(user_ids), 20):
            results = await asyncio.gather(
                *(fetch(user_id) for user_id in user_ids[start : start + 20])
            )
            profiles.update({user_id: value for user_id, value in results if value})
            await asyncio.sleep(0.75)
    return profiles


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
        profiles = await _current_telegram_profiles(
            [history.telegram_user_id for history, _ in rows]
        )
        receipt_matches = await _receipt_phone_matches(rows)
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
                    "matched_phone_hashes": receipt_matches.get(history.id, []),
                    **profiles.get(history.telegram_user_id, {}),
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

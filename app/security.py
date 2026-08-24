from __future__ import annotations

import base64
import hashlib
import hmac


def base36_encode(value: int) -> str:
    if value < 0:
        raise ValueError("value must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


def base36_decode(value: str) -> int:
    return int(value, 36)


class TokenSigner:
    def __init__(self, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("CALLBACK_SECRET must be at least 16 characters")
        self.key = secret.encode("utf-8")

    def _signature(self, purpose: str, encoded_id: str) -> str:
        digest = hmac.new(self.key, f"{purpose}:{encoded_id}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest[:6]).decode().rstrip("=")

    def sign_id(self, purpose: str, value: int) -> str:
        encoded = base36_encode(value)
        return f"{encoded}.{self._signature(purpose, encoded)}"

    def verify_id(self, purpose: str, token: str) -> int | None:
        try:
            encoded, signature = token.split(".", 1)
            expected = self._signature(purpose, encoded)
            if not hmac.compare_digest(signature, expected):
                return None
            return base36_decode(encoded)
        except (ValueError, TypeError):
            return None

    def sign_start_id(self, purpose: str, value: int) -> str:
        """Sign an ID using only characters allowed in Telegram start parameters."""
        return self.sign_id(purpose, value).replace(".", "-", 1)

    def verify_start_id(self, purpose: str, token: str) -> int | None:
        """Verify a Telegram-safe start parameter token."""
        encoded, separator, signature = token.partition("-")
        if not separator or not encoded or not signature:
            return None
        return self.verify_id(purpose, f"{encoded}.{signature}")

    def sign_values(self, purpose: str, *values: int) -> str:
        if not values:
            raise ValueError("at least one value is required")
        encoded = ".".join(base36_encode(value) for value in values)
        return f"{encoded}.{self._signature(purpose, encoded)}"

    def verify_values(self, purpose: str, token: str, *, count: int) -> tuple[int, ...] | None:
        try:
            encoded, signature = token.rsplit(".", 1)
            parts = encoded.split(".")
            if len(parts) != count:
                return None
            expected = self._signature(purpose, encoded)
            if not hmac.compare_digest(signature, expected):
                return None
            return tuple(base36_decode(part) for part in parts)
        except (ValueError, TypeError):
            return None

from __future__ import annotations

import re


_AMOUNT = r"(?P<amount>\d{1,3}(?:[\s\u00a0.,]\d{3})+|\d{3,7})"
_PATTERNS = (
    re.compile(rf"(?:депозит|залог)\s*[:\-]?\s*{_AMOUNT}", re.IGNORECASE),
    re.compile(rf"\+\s*{_AMOUNT}\s*(?:депозит|залог)", re.IGNORECASE),
)


def parse_deposit(text: str | None) -> int | None:
    if not text:
        return None
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group("amount"))
        if digits:
            value = int(digits)
            if 0 < value <= 10_000_000:
                return value
    return None

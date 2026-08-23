from __future__ import annotations

import re


def normalize_kg_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "996" + digits[1:]
    elif len(digits) == 9:
        digits = "996" + digits
    elif len(digits) == 12 and digits.startswith("996"):
        pass
    else:
        raise ValueError("Unsupported Kyrgyz phone number")
    return "+" + digits


def mask_phone(value: str) -> str:
    canonical = normalize_kg_phone(value)
    return canonical[:4] + "******" + canonical[-3:]


def display_phone(value: str) -> str:
    canonical = normalize_kg_phone(value)
    digits = canonical[1:]
    return f"+{digits[:3]} {digits[3:6]} {digits[6:9]} {digits[9:]}"

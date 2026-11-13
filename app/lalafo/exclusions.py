"""Permanent publication exclusions for unsuitable Lalafo advertisements.

Individual withdrawn cards are identified by ID. Salut is excluded by
source metadata, without inspecting image contents.
"""

from __future__ import annotations

import json
import re

from app.lalafo.models import LalafoAd


# Explicitly withdrawn advertisements must never be sent to Telegram again.
# The Filarmoniya IDs are the original source and its managed public copy for
# the 25,000 KGS card with the bouquet photo.
PERMANENTLY_EXCLUDED_LALAFO_IDS = frozenset(
    {
        115809037,
        115884595,
        112298605,
        97832535,
    }
)


def is_permanently_excluded(lalafo_id: int) -> bool:
    """Return whether this source advertisement is blocked from publication."""
    return lalafo_id in PERMANENTLY_EXCLUDED_LALAFO_IDS



EXCLUDED_CONTACT_SUFFIXES = frozenset({"703108500", "703223711"})
EXCLUDED_CONTACT_RE = re.compile(r"(?<!\d)(?:\+?996[\s().-]*)?0?703[\s().-]*(?:108[\s().-]*500|223[\s().-]*711)(?!\d)")

def is_excluded_contact(ad) -> bool:
    digits = re.sub(r"\D", "", str(getattr(ad, "phone", "") or ""))
    if digits in {prefix+number for prefix in ("", "0", "996") for number in EXCLUDED_CONTACT_SUFFIXES}:
        return True
    return bool(EXCLUDED_CONTACT_RE.search(" ".join(str(getattr(ad, field, "") or "") for field in ("phone", "source_title", "source_description"))))

def is_excluded_agency(ad: LalafoAd) -> bool:
    """Exclude Salut using source metadata, without downloading photographs."""
    if is_excluded_contact(ad):
        return True
    text = " ".join((
        ad.source_url, ad.source_title, ad.source_description,
        json.dumps(ad.source_params, ensure_ascii=False),
        " ".join(ad.photo_urls),
    )).casefold()
    return bool(re.search(r"(?<![\w-])salut\.kg\b|\bсалют\b|\bsalut\b", text))

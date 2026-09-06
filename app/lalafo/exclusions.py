"""Permanent publication exclusions for unsuitable Lalafo advertisements.

Keep this list deliberately small and ID-based: a title, photo, or price can
change, whereas Lalafo's advertisement ID identifies the exact source card.
"""

from __future__ import annotations


# The Tunguch 20,000 KGS apartment (gray sectional sofa) was explicitly
# withdrawn by the operator and must never be sent to Telegram again.
PERMANENTLY_EXCLUDED_LALAFO_IDS = frozenset({115809037})


def is_permanently_excluded(lalafo_id: int) -> bool:
    """Return whether this source advertisement is blocked from publication."""
    return lalafo_id in PERMANENTLY_EXCLUDED_LALAFO_IDS

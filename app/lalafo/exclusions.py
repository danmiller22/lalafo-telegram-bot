"""Permanent publication exclusions for unsuitable Lalafo advertisements.

Keep this list deliberately small and ID-based: a title, photo, or price can
change, whereas Lalafo's advertisement ID identifies the exact source card.
"""

from __future__ import annotations


# Explicitly withdrawn advertisements must never be sent to Telegram again.
# The Filarmoniya IDs are the original source and its managed public copy for
# the 25,000 KGS card with the bouquet photo.
PERMANENTLY_EXCLUDED_LALAFO_IDS = frozenset(
    {
        115809037,
        115884595,
        112298605,
    }
)


def is_permanently_excluded(lalafo_id: int) -> bool:
    """Return whether this source advertisement is blocked from publication."""
    return lalafo_id in PERMANENTLY_EXCLUDED_LALAFO_IDS

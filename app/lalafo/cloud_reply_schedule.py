"""Keep cloud greetings outside the owner's PC working hours."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo('America/Chicago')


def cloud_reply_allowed(now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('An aware timestamp is required')
    return not 7 <= current.astimezone(CHICAGO).hour < 17

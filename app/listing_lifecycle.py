"""The shared 48-hour validity window for published apartment cards."""
from datetime import datetime, timedelta, timezone

MAX_LISTING_AGE_DAYS = 2


def listing_expired(apartment, *, now: datetime | None = None) -> bool:
    published = apartment.published_at
    if published is None:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= published + timedelta(days=MAX_LISTING_AGE_DAYS)

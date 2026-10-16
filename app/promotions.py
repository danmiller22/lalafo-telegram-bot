"""Durable, explicitly requested card promotion and acknowledged-send accounting."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, union_all

from app.telegram.formatting import is_supported_source
from app.models import Apartment, ApartmentInventoryQueue, RequestedCardPromotion, RequestedCardPublication

DAILY_REQUESTED_CARD_LIMIT = 3
REQUESTED_CARD_GAP = timedelta(hours=6)
REQUESTED_WINDOW_PREFIX = 'requested-card:'


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def publication_window(start, end):
    events = select(RequestedCardPublication.apartment_id).where(
        RequestedCardPublication.published_at >= start,
        RequestedCardPublication.published_at < end,
    )
    return union_all(
        select(Apartment.id.label('apartment_id'), Apartment.district, Apartment.seller_type,
               Apartment.price, Apartment.published_at).where(
            Apartment.publication_status == 'published', Apartment.published_at >= start,
            Apartment.published_at < end, Apartment.id.not_in(events),
        ),
        select(RequestedCardPublication.apartment_id, RequestedCardPublication.district,
               RequestedCardPublication.seller_type, RequestedCardPublication.price,
               RequestedCardPublication.published_at).where(
            RequestedCardPublication.published_at >= start,
            RequestedCardPublication.published_at < end,
        ),
    ).subquery()


async def publications_in_window(session, start, end):
    records = publication_window(start, end)
    return list((await session.execute(select(records).order_by(records.c.published_at.desc()))).all())


async def record_delivery(session, apartment, *, chat_id, message_id, published_at):
    existing = await session.scalar(select(RequestedCardPublication).where(
        RequestedCardPublication.telegram_chat_id == chat_id,
        RequestedCardPublication.telegram_message_id == message_id,
    ))
    if existing is not None:
        if existing.apartment_id != apartment.id:
            raise RuntimeError('Publication receipt belongs to another apartment')
        return False
    session.add(RequestedCardPublication(
        apartment_id=apartment.id, telegram_chat_id=chat_id, telegram_message_id=message_id,
        published_at=published_at, district=apartment.district, seller_type=apartment.seller_type,
        price=apartment.price,
    ))
    await session.flush()
    return True


async def register_requested(session, apartment, *, now):
    promotion = await session.get(RequestedCardPromotion, apartment.id)
    if promotion is None:
        promotion = RequestedCardPromotion(apartment_id=apartment.id, requested_at=now, enabled=True)
        session.add(promotion)
    else:
        promotion.requested_at = now
        promotion.enabled = True
    promotion.expires_at = now + timedelta(days=1)
    # Preserve the preceding acknowledged send before mark_published overwrites
    # the apartment's latest message. This includes an ordinary initial card.
    if apartment.telegram_message_id and apartment.telegram_chat_id and apartment.published_at:
        await record_delivery(session, apartment, chat_id=apartment.telegram_chat_id,
                              message_id=apartment.telegram_message_id, published_at=apartment.published_at)
    await session.flush()


async def promotion_work(session, *, now, day_start, day_end):
    """Return due work and reserve future feasible repeats inside today's quota."""
    from app.managed_cards import managed_bindings
    bindings = await managed_bindings(session)
    rows = list((await session.execute(select(RequestedCardPromotion, Apartment).join(
        Apartment, Apartment.id == RequestedCardPromotion.apartment_id,
    ).where(or_(RequestedCardPromotion.enabled.is_(True),
                RequestedCardPromotion.apartment_id.in_(bindings)), Apartment.active.is_(True),
            RequestedCardPromotion.expires_at > now,
    ))).all())
    ids = [apartment.id for _, apartment in rows]
    stats = {}
    if ids:
        result = await session.execute(select(
            RequestedCardPublication.apartment_id,
            func.max(RequestedCardPublication.published_at),
            func.count(RequestedCardPublication.id).filter(
                (RequestedCardPublication.published_at >= day_start) &
                (RequestedCardPublication.published_at < day_end)),
            func.count(RequestedCardPublication.id).filter(
                RequestedCardPublication.published_at >= RequestedCardPromotion.requested_at),
        ).join(RequestedCardPromotion, RequestedCardPromotion.apartment_id == RequestedCardPublication.apartment_id)
         .where(RequestedCardPublication.apartment_id.in_(ids)).group_by(RequestedCardPublication.apartment_id))
        stats = {row[0]: (row[1], row[2], row[3]) for row in result}
    due = []
    reserved = 0
    for promotion, apartment in rows:
        if (not apartment.phone and apartment.id not in bindings) or not apartment.photo_urls or not is_supported_source(apartment):
            continue
        recent, count, campaign_count = stats.get(apartment.id, (None, 0, 0))
        latest = max([utc(v) for v in (recent, apartment.published_at) if v is not None], default=utc(promotion.requested_at))
        remaining = max(0, min(DAILY_REQUESTED_CARD_LIMIT - count,
                               DAILY_REQUESTED_CARD_LIMIT - campaign_count))
        next_send = max(now, latest + REQUESTED_CARD_GAP)
        end = min(day_end, utc(promotion.expires_at)) if promotion.expires_at else day_end
        possible = 0
        cursor = next_send
        while possible < remaining and cursor < end:
            possible += 1
            cursor += REQUESTED_CARD_GAP
        reserved += possible
        if remaining and next_send <= now and now < end:
            due.append((latest, apartment))
    return sorted(due, key=lambda row: row[0]), reserved


async def queue_due_promotions(session, *, now, day_start, day_end, managed_ids=()):
    due, reserved = await promotion_work(session, now=now, day_start=day_start, day_end=day_end)
    from app.managed_cards import managed_bindings
    bindings = await managed_bindings(session)
    # Only the dedicated worker can queue a managed card, and it claims that
    # single row in the same transaction. Old web workers must never see a
    # leftover managed row in queued/retry state.
    due = [(latest, apartment) for latest, apartment in due
           if apartment.id not in bindings or apartment.id in managed_ids]
    due_ids = {apartment.id for _, apartment in due}
    old_rows = list((await session.scalars(select(ApartmentInventoryQueue).where(
        ApartmentInventoryQueue.status == 'queued',
        ApartmentInventoryQueue.window_key.startswith(REQUESTED_WINDOW_PREFIX),
    ))).all())
    for row in old_rows:
        if row.apartment_id not in due_ids and not row.last_error:
            row.status = 'skipped'
            row.last_error = 'requested_waiting'
    for _, apartment in due:
        row = await session.scalar(select(ApartmentInventoryQueue).where(
            ApartmentInventoryQueue.apartment_id == apartment.id))
        if row is not None:
            if row.status == 'publishing' or (row.status == 'queued' and row.last_error and utc(row.scheduled_at) > now):
                continue
            if row.last_error == 'watermarked_photos':
                continue
            # A failed refresh must not be retried recursively in one cycle.
            if row.status == 'skipped' and (row.last_error or '').startswith('stale_'):
                seen = apartment.last_seen_at or apartment.updated_at
                if utc(seen) <= utc(row.updated_at) and utc(row.updated_at) > now - timedelta(minutes=20):
                    continue
        if row is None:
            row = ApartmentInventoryQueue(apartment_id=apartment.id)
            session.add(row)
        row.scheduled_at = now
        row.window_key = REQUESTED_WINDOW_PREFIX + day_start.date().isoformat()
        row.sequence = 0
        row.status = 'queued'
        row.claimed_at = None
        row.published_at = None
        row.last_error = None
        row.attempts = 0
    await session.flush()
    return reserved


async def requested_send_allowed(sessions, *, apartment_id, now, day_start, day_end):
    async with sessions() as session:
        due, _ = await promotion_work(session, now=now, day_start=day_start, day_end=day_end)
        return any(apartment.id == apartment_id for _, apartment in due)

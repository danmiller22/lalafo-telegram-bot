"""Daily medians of observed, eligible Lalafo inventory, never advertised as market-wide."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from app.models import Apartment, RentalFeedState
from app.state import normalized_district, ListingIndex
from app.lalafo.exclusions import is_excluded_contact, is_excluded_agency, is_permanently_excluded
from scripts.scrape_publish import apartment_to_ad


def market_key(rooms, district):
    return rooms + ':' + normalized_district(district or '')


def value_rank(price, rooms, district, medians):
    reference = medians.get(market_key(rooms, district))
    # Insufficient district observations: retain the existing price ranking.
    if reference is None:
        return 1
    return 0 if price <= reference * 1.1 else 2


async def daily_medians(session, *, now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo('Asia/Bishkek')).date().isoformat()
    if session.bind.dialect.name == 'postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(731290514)'))
    state = await session.get(RentalFeedState, 'district_medians')
    if state and state.value.get('day') == day:
        return state.value['medians']
    rows = list((await session.scalars(select(Apartment).where(
        Apartment.lalafo_id > 0, Apartment.active.is_(True),
        Apartment.availability_status != 'unavailable',
        Apartment.last_seen_at >= now - timedelta(days=30),
        Apartment.rooms.in_(['studio', '1']), Apartment.price.between(25000, 45000),
    ).order_by(Apartment.last_seen_at.desc()))).all())
    groups = defaultdict(list)
    unique = []
    index = ListingIndex([])
    for row in rows:
        ad = apartment_to_ad(row)
        if (is_permanently_excluded(row.lalafo_id) or is_excluded_contact(ad)
                or is_excluded_agency(ad) or index.contains(ad)):
            continue
        unique.append(row)
        index.add(row)
        if row.district:
            groups[market_key(row.rooms, row.district)].append(row.price)
    medians = {key: median(prices) for key, prices in groups.items() if len(prices) >= 5}
    value = {'day': day, 'updated_at': now.isoformat(), 'sample_count': len(unique),
             'medians': medians, 'minimum_samples': 5, 'lookback_days': 30,
             'scope': 'eligible_observed_lalafo_inventory'}
    if state:
        state.value = value
    else:
        if session.bind.dialect.name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
            await session.execute(insert(RentalFeedState).values(key='district_medians', value=value).on_conflict_do_nothing(index_elements=['key']))
        else:
            session.add(RentalFeedState(key='district_medians', value=value))
    await session.flush()
    return medians

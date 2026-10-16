"""Explicit mappings from original Telegram contact cards to the owner's ads."""
import json
from urllib.parse import urlsplit

from sqlalchemy import select

from app.models import LalafoAutoReplyMeta

PREFIX = 'managed_requested_card:'


def validate_binding(data):
    url = urlsplit(data['url'])
    if (int(data['profile_id']) <= 0 or int(data['ad_id']) <= 0
            or url.scheme != 'https' or url.hostname != 'lalafo.kg'
            or not url.path.endswith(f"-id-{int(data['ad_id'])}")
            or url.query or url.fragment):
        raise ValueError('Invalid managed advertisement binding')
    return data


async def managed_bindings(session):
    rows = await session.scalars(select(LalafoAutoReplyMeta).where(
        LalafoAutoReplyMeta.key.startswith(PREFIX)))
    result = {}
    for row in rows:
        data = validate_binding(json.loads(row.value))
        if data.get('enabled') is True:
            result[int(row.key[len(PREFIX):])] = data
    return result


class ManagedCardUnavailable(ValueError):
    pass


async def refresh_managed_card(client, binding, stored):
    """Read public terms only. Hidden telephone fields are never accessed."""
    binding = validate_binding(binding)
    data = await client._get_json(
        f"https://lalafo.kg/api/search/v3/feed/details/{binding['ad_id']}?expand=url")
    required = ('id', 'user_id', 'status_id', 'category_id', 'city_id', 'currency', 'price')
    if any(data.get(key) is None for key in required):
        raise ValueError('managed_ad_public_data_unknown')
    if (data.get('id') != binding['ad_id'] or data.get('user_id') != binding['profile_id']
            or data.get('status_id') != 2):
        raise ManagedCardUnavailable('managed_ad_not_active_or_owner_changed')
    if (data.get('category_id') != 2044 or data.get('city_id') != 103184
            or str(data.get('currency')).upper() != 'KGS'):
        raise ManagedCardUnavailable('managed_ad_category_or_currency_changed')
    params = {p['id']: p for p in data.get('params', [])}
    def selected(param_id):
        return next((v for v in params.get(param_id, {}).get('values', [])
                     if v.get('selected') is True), {})
    # Public feed params use value/values_ids; owner-visible forms instead
    # expose dropdown choices with a selected flag.
    room_param = params.get(69, {})
    room_label = str(room_param.get('value') or selected(69).get('value') or '').strip().casefold()
    room_id = room_param.get('value_id') or selected(69).get('id')
    rooms = ({2773: '1', 15496: 'studio'}.get(room_id)
             or {'1 комната': '1', 'студия': 'studio'}.get(room_label))
    if not rooms and room_id not in (2774, 2775, 2776, 2777, 2781, 30624):
        raise ValueError('managed_ad_room_data_unknown')
    if not rooms:
        raise ManagedCardUnavailable('managed_ad_room_type_changed')
    photos = [im['original_url'] for im in data.get('images', []) if im.get('original_url')]
    if not photos:
        raise ValueError('managed_ad_photos_unknown')
    deposit = params.get(947, {}).get('value')
    deposit = int(deposit) if str(deposit or '').isdigit() and int(deposit) > 1 else None
    return stored.model_copy(update={
        'price': int(data['price']), 'rooms': rooms,
        'district': params.get(357, {}).get('value') or selected(357).get('value') or stored.district,
        'deposit': deposit, 'photo_urls': photos,
        'source_description': str(data.get('description') or ''),
    })

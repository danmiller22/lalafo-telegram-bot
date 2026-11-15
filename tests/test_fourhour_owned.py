from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import LalafoAutoReplyMeta
from tests.helpers import make_ad
from tools.publish_fourhour_owned import publish, validate_packet, PROFILE, CHAT


def packet(now):
    return {'profile_id':PROFILE, 'verified_at':now.isoformat(),
            'cycle':int(now.timestamp())//14400, 'cards':[{
                'id':117970046,'user_id':PROFILE,'status_id':2,'category_id':2044,
                'city_id':103184,'currency':'KGS','rooms':'1','price':30000,
                'district':'Моссовет','photo_urls':['https://img.example/room.jpg'],
                'source_description':'1-комнатная квартира\nБишкек\nМоссовет\n30 000 сом'}]}


@pytest.mark.parametrize('field,value',[('user_id',1),('status_id',11),('price',0),
    ('source_description','Ориентир: 30 000 сом'),('photo_urls',[])])
def test_fourhour_rejects_invalid_snapshots(field,value):
    now=datetime.now(timezone.utc); data=packet(now);data['cards'][0][field]=value
    with pytest.raises(ValueError):validate_packet(data,now)


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous',[False,True])
async def test_fourhour_never_replays_reserved_or_acknowledged_send(repositories, ambiguous):
    apartments,_,sessions=repositories
    now=datetime.now(timezone.utc);data=packet(now)
    original=await apartments.upsert_discovered(make_ad(lalafo_id=118170246))
    await apartments.save_managed_lalafo_source(managed_lalafo_ad_id=117970046,
        managed_lalafo_ad_url='https://lalafo.kg/bishkek/ads/test-id-117970046',apartment=original)
    publisher=SimpleNamespace(publish=AsyncMock(return_value=SimpleNamespace(message_id=701,date=now)))
    if ambiguous:publisher.publish.side_effect=RuntimeError('ambiguous')
    if ambiguous:
        with pytest.raises(RuntimeError):await publish(data,sessions,publisher,now)
        with pytest.raises(RuntimeError,match='reserved_unverified'):await publish(data,sessions,publisher,now)
    else:
        await publish(data,sessions,publisher,now)
        await publish(data,sessions,publisher,now)
    assert publisher.publish.await_count==1
    async with sessions() as session:
        marker=await session.get(LalafoAutoReplyMeta,f"owned4h:{data['cycle']}:117970046")
        assert marker.value==('reserved_unverified' if ambiguous else 'published:701')

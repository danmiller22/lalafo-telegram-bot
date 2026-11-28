import io
import random
from unittest.mock import AsyncMock
import httpx
import pytest
from PIL import Image,ImageDraw
from app.photo_match import signature,pair_count,PhotoMatcher


def photo(seed,quality=92):
    rng=random.Random(seed)
    image=Image.new('RGB',(160,160))
    draw=ImageDraw.Draw(image)
    for x in range(0,160,20):
        for y in range(0,160,20):
            draw.rectangle((x,y,x+19,y+19),fill=tuple(rng.randrange(256) for _ in range(3)))
    buffer=io.BytesIO();image.save(buffer,format='JPEG',quality=quality)
    return buffer.getvalue()


def test_reencoded_photos_match_but_two_distinct_frames_are_required():
    a,b=signature(photo(1)),signature(photo(2))
    reencoded=[signature(photo(2,80)),signature(photo(1,80))]
    assert pair_count([a,b],reencoded)==2
    assert pair_count([a,a],[a,a])==1
    assert pair_count([a,b],[signature(photo(3)),signature(photo(4))])==0
    flat=io.BytesIO();Image.new('RGB',(160,160),'white').save(flat,format='JPEG')
    assert signature(flat.getvalue()) is None


@pytest.mark.asyncio
async def test_hashes_survive_a_new_matcher_without_retaining_photos_or_urls(repositories):
    _,_,sessions=repositories
    requests=[]
    async def image(request):
        requests.append(request)
        return httpx.Response(200,content=photo(1),headers={'content-type':'image/jpeg'})
    url='https://img5.lalafo.com/unique-asset.jpg'
    async with httpx.AsyncClient(transport=httpx.MockTransport(image)) as client:
        async with PhotoMatcher(sessions,client=client) as matcher:
            first=await matcher.get(url)
        async with PhotoMatcher(sessions,client=client) as matcher:
            assert await matcher.get(url)==first
            assert await matcher.get('http://127.0.0.1/private') is None
    assert len(requests)==1
    from app.models import RentalFeedState
    from sqlalchemy import select
    import json
    async with sessions() as session:
        row=await session.scalar(select(RentalFeedState).where(RentalFeedState.key.startswith('photo_v1:')))
        assert url not in json.dumps(row.value)
        assert 'signature' in row.value


@pytest.mark.asyncio
async def test_visual_match_does_not_bypass_price_room_or_location(repositories):
    from app.profile_originals import verified_photos
    from tests.helpers import make_ad
    matcher=type('Matcher',(),{'matches':AsyncMock(return_value=True)})()
    own=make_ad(photo_urls=['https://img5.lalafo.com/own-a.jpg','https://img5.lalafo.com/own-b.jpg'])
    candidate=make_ad(lalafo_id=900,photo_urls=['https://img5.lalafo.com/new-a.jpg','https://img5.lalafo.com/new-b.jpg'])
    assert await verified_photos(own,candidate,matcher)
    assert not await verified_photos(own,candidate.model_copy(update={'price':45000}),matcher)
    assert not await verified_photos(own,candidate.model_copy(update={'district':'Джал'}),matcher)
    assert not await verified_photos(own,candidate.model_copy(update={'rooms':'2'}),matcher)
    matcher.matches.assert_awaited_once()

@pytest.mark.asyncio
async def test_matching_version_refreshes_scan_once_without_resetting_delivery_state(repositories):
    from app.rental_feed import claim_cycle,finish_cycle
    _,_,sessions=repositories
    assert await claim_cycle(sessions,'version_test',version=1)
    await finish_cycle(sessions,'version_test',{})
    assert not await claim_cycle(sessions,'version_test',version=1)
    assert await claim_cycle(sessions,'version_test',version=2)


@pytest.mark.asyncio
async def test_special_original_does_not_reset_ordinary_publication_clock(repositories):
    from datetime import datetime,timedelta,timezone
    from app.models import LalafoAutoReplyMeta
    from app.promotions import external_repeat_receipt_key,publication_counts_in_window,record_delivery
    from tests.helpers import make_ad
    apartments,_,sessions=repositories
    now=datetime.now(timezone.utc)
    ordinary=await apartments.upsert_discovered(make_ad(lalafo_id=902,photo_urls=['https://img5.lalafo.com/ordinary.jpg']))
    await apartments.mark_published(ordinary.id,chat_id=-1001,message_id=902,acknowledged_at=now-timedelta(minutes=14))
    special=await apartments.upsert_discovered(make_ad(lalafo_id=903,phone='+996555000903',photo_urls=['https://img5.lalafo.com/special.jpg']))
    await apartments.mark_published(special.id,chat_id=-1001,message_id=903,acknowledged_at=now)
    async with sessions.begin() as session:
        await record_delivery(session,special,chat_id=-1001,message_id=903,published_at=now)
        session.add(LalafoAutoReplyMeta(key=external_repeat_receipt_key(-1001,903),value='hosted-profile-original'))
    async with sessions() as session:
        counts=await publication_counts_in_window(session,now-timedelta(hours=1),now+timedelta(hours=1))
        assert counts.total==2 and counts.ordinary==1
        assert abs((counts.latest-counts.latest_ordinary).total_seconds())==14*60

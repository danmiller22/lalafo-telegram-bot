import pytest

from app.lalafo.models import SearchAd
from scripts.scrape_publish import fetch_detail_batch
from tests.helpers import make_ad


class DetailClientStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def detail(self, url: str):
        self.calls.append(url)
        return make_ad(lalafo_id=int(url.rsplit("/", 1)[-1]))


@pytest.mark.asyncio
async def test_detail_batch_fetches_duplicate_search_ids_once():
    client = DetailClientStub()
    search_ads = [
        SearchAd(lalafo_id=101, detail_url="https://example.test/101"),
        SearchAd(lalafo_id=101, detail_url="https://example.test/101"),
        SearchAd(lalafo_id=102, detail_url="https://example.test/102"),
    ]

    details = await fetch_detail_batch(search_ads, [client])

    assert client.calls == ["https://example.test/101", "https://example.test/102"]
    assert [search_ad.lalafo_id for search_ad, _ in details] == [101, 102]


@pytest.mark.asyncio
async def test_detail_cache_avoids_repeat_requests_across_sources_but_refreshes_next_cycle():
    client = DetailClientStub()
    ad = SearchAd(lalafo_id=101, detail_url="https://example.test/101")
    cache = {}
    first = await fetch_detail_batch([ad], [client], cache=cache)
    second = await fetch_detail_batch([ad], [client], cache=cache)
    assert first == second
    assert len(client.calls) == 1
    await fetch_detail_batch([ad], [client], cache={})
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_detail_cache_does_not_repeat_hidden_phone_requests():
    from unittest.mock import AsyncMock
    from app.lalafo.parser import LalafoParseError
    client = DetailClientStub()
    client.detail = AsyncMock(side_effect=LalafoParseError("Advertisement owner hides the phone number"))
    ad = SearchAd(lalafo_id=101, detail_url="https://example.test/101")
    cache = {}
    assert (await fetch_detail_batch([ad], [client], cache=cache))[0][1] is None
    assert (await fetch_detail_batch([ad], [client], cache=cache))[0][1] is None
    client.detail.assert_awaited_once()

@pytest.mark.asyncio
async def test_batch_deadline_keeps_completed_replies_from_healthy_worker():
    import asyncio
    class SlowClient:
        async def detail(self,url):await asyncio.Event().wait()
    ads=[SearchAd(lalafo_id=i,detail_url=f'https://example.test/{i}') for i in [101,102]]
    cache={}
    result=await asyncio.wait_for(fetch_detail_batch(ads,[SlowClient(),DetailClientStub()],cache=cache,batch_timeout_seconds=0.03),timeout=0.5)
    assert result[0][1] is None
    assert result[1][1].lalafo_id==102
    assert cache[102].lalafo_id==102

@pytest.mark.asyncio
async def test_single_slow_detail_does_not_block_next_candidate_on_same_worker():
    import asyncio
    class Client(DetailClientStub):
        async def detail(self,url):
            if url.endswith('/101'):await asyncio.Event().wait()
            return await super().detail(url)
    ads=[SearchAd(lalafo_id=i,detail_url=f'https://example.test/{i}') for i in [101,102]]
    result=await asyncio.wait_for(fetch_detail_batch(ads,[Client()],detail_timeout_seconds=0.01),timeout=0.5)
    assert result[0][1] is None
    assert result[1][1].lalafo_id==102


def test_slow_owner_queries_leave_time_for_agency_sources():
    from scripts.scrape_publish import crawl_source_budget
    args=dict(started_at=0,seconds=180,owner_count=4,source_count=10)
    assert crawl_source_budget(**args,now=30,source_index=0)==(0,87)
    assert crawl_source_budget(**args,now=45,source_index=1)==(1,72)
    assert crawl_source_budget(**args,now=163,source_index=1)==(4,17)
    assert crawl_source_budget(**args,now=180,source_index=4)==(4,0)


def test_unused_owner_time_remains_available_to_agencies():
    from scripts.scrape_publish import crawl_source_budget
    assert crawl_source_budget(started_at=0,now=30,seconds=180,source_index=4,owner_count=4,source_count=10)==(4,150)

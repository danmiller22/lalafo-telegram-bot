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

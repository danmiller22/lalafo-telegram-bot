"""Bounded first-party JSON reads. Access restrictions are terminal for a cycle."""
import asyncio
import random
import httpx
from app.lalafo.client import LalafoClient
from app.lalafo.parser import parse_detail_data


class PublicFeed:
    def __init__(self, *, proxy=None, proxy_urls=""):
        self.routes=[proxy] if proxy else [None]+[p.strip() for p in proxy_urls.split(",") if p.strip()]
        self.route_index=0
        self.client=self.make_client()

    def make_client(self):
        return httpx.AsyncClient(timeout=httpx.Timeout(10,connect=5),proxy=self.routes[self.route_index],
            follow_redirects=False,headers={'User-Agent':'Mozilla/5.0', 'device':'pc',
            'language':'ru_RU','country-id':'12','Accept':'application/json'})

    async def __aenter__(self):return self
    async def __aexit__(self,*args):await self.client.aclose()

    async def get(self,path,params=None):
        for attempt in range(3):
            try:
                response=await self.client.get('https://lalafo.kg'+path,params=params)
                # In particular 403/429/security challenges must never trigger
                # proxy/header rotation or third-party relay fallback.
                if response.status_code < 500:
                    response.raise_for_status()
                    data=response.json()
                    if not isinstance(data,dict):raise ValueError('InvalidPublicFeed')
                    return data
                response.raise_for_status()
            except (httpx.TransportError,httpx.HTTPStatusError) as exc:
                if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code < 500:raise
                if attempt==2:raise
                if isinstance(exc,httpx.TransportError) and len(self.routes)>1:
                    await self.client.aclose()
                    self.route_index=(self.route_index+1)%len(self.routes)
                    self.client=self.make_client()
                await asyncio.sleep((2**attempt)+random.random())

    async def search(self,route,page=1):
        params=LalafoClient._search_params(route,page,None)
        return await self.get('/api/search/v3/feed/search',params)

    async def detail(self,source_id):
        raw=await self.get('/api/search/v3/feed/details/'+str(source_id),{'expand':'url'})
        if raw.get('id')!=source_id or raw.get('status_id')!=2:
            raise ValueError('ListingNotActive')
        return raw


def parsed_public_ad(raw):
    url=raw.get('url') or ''
    if url.startswith('/'):url='https://lalafo.kg'+url
    if not url.startswith(('https://lalafo.kg/','https://www.lalafo.kg/')):
        raise ValueError('MissingPublicSourceURL')
    return parse_detail_data(raw,source_url=url)

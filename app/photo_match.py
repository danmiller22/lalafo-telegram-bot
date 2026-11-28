"""Conservative content matching for re-uploaded Lalafo apartment photos."""
import asyncio
import hashlib
import io
from datetime import datetime,timezone
from urllib.parse import urlsplit

import httpx
from PIL import Image,ImageOps,ImageStat,UnidentifiedImageError
from app.models import RentalFeedState


def signature(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>20_000_000:raise ValueError('ImageTooLarge')
        image=ImageOps.exif_transpose(image).convert('RGB')
        grey=image.convert('L')
        if ImageStat.Stat(grey.resize((32,32))).stddev[0]<12:return None
        small=grey.resize((9,8),Image.Resampling.LANCZOS)
        px=list(small.getdata())
        dh=sum(int(px[y*9+x]>px[y*9+x+1])<<(y*8+x) for y in range(8) for x in range(8))
        if not 8<=dh.bit_count()<=56:return None
        px=list(grey.resize((8,8),Image.Resampling.LANCZOS).getdata())
        mean=sum(px)/64
        ah=sum(int(v>mean)<<i for i,v in enumerate(px))
        color=[round(v) for v in ImageStat.Stat(image.resize((16,16))).mean]
        return {'dh':f'{dh:016x}','ah':f'{ah:016x}','color':color}


def near(a,b):
    return (a is not None and b is not None
        and (int(a['dh'],16)^int(b['dh'],16)).bit_count()<=4
        and (int(a['ah'],16)^int(b['ah'],16)).bit_count()<=4
        and max(abs(x-y) for x,y in zip(a['color'],b['color']))<=15)


def pair_count(left,right):
    def unique(values):
        result=[]
        for v in values:
            if v is not None and not any(near(v,other) for other in result):result.append(v)
        return result
    left,right=unique(left),unique(right)
    # Maximum bipartite matching: one original image cannot stand in for two
    # photos, including when the same photo was reuploaded under several URLs.
    assigned={}
    def assign(i,seen):
        for j,b in enumerate(right):
            if j in seen or not near(left[i],b):continue
            seen.add(j)
            if j not in assigned or assign(assigned[j],seen):
                assigned[j]=i
                return True
        return False
    return sum(assign(i,set()) for i in range(len(left)))


class PhotoMatcher:
    def __init__(self,sessions,*,proxy=None,client=None):
        self.sessions=sessions
        self.client=client or httpx.AsyncClient(proxy=proxy,timeout=8,follow_redirects=False)
        self.owns_client=client is None
        self.pending={}
        self.stats={"cache_hits":0,"downloads":0,"errors":0}
        self.limit=asyncio.Semaphore(3)

    async def __aenter__(self):return self
    async def __aexit__(self,*args):
        for task in self.pending.values():
            if not task.done():task.cancel()
        await asyncio.gather(*self.pending.values(),return_exceptions=True)
        if self.owns_client:await self.client.aclose()

    async def get(self,url):
        parsed=urlsplit(url)
        host=(parsed.hostname or '').casefold()
        if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
                or not (host=='lalafo.com' or host.endswith('.lalafo.com'))):return None
        if url not in self.pending:
            self.pending[url]=asyncio.create_task(self._get(url))
        return await self.pending[url]

    async def _get(self,url):
        key='photo_v1:'+hashlib.sha256(url.encode()).hexdigest()[:48]
        async with self.sessions() as session:
            state=await session.get(RentalFeedState,key)
            if state is not None:
                self.stats['cache_hits']+=1
                return state.value.get('signature')
        async with self.limit:
            try:
                self.stats['downloads']+=1
                async with self.client.stream('GET',url) as response:
                    response.raise_for_status()
                    if not response.headers.get('content-type','').startswith('image/'):return None
                    data=bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>5*1024*1024:return None
                value=await asyncio.to_thread(signature,bytes(data))
            except (httpx.HTTPError,ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError):
                self.stats["errors"]+=1
                return None
        # No image URL, phone or original photo is retained in the database.
        payload={'signature':value,'computed_at':datetime.now(timezone.utc).isoformat()}
        async with self.sessions.begin() as session:
            if session.bind.dialect.name=='postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            await session.execute(insert(RentalFeedState).values(key=key,value=payload).on_conflict_do_nothing(index_elements=['key']))
        return value

    async def matches(self,left,right):
        a=await asyncio.gather(*(self.get(url) for url in list(dict.fromkeys(left))[:4]))
        b=await asyncio.gather(*(self.get(url) for url in list(dict.fromkeys(right))[:4]))
        return pair_count(a,b)>=2

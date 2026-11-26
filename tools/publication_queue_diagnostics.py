import asyncio, json
from io import BytesIO
from pathlib import Path
from collections import defaultdict
import httpx
from PIL import Image, ImageOps
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment

def digest(raw):
    with Image.open(BytesIO(raw)) as image:
        image = ImageOps.exif_transpose(image).convert("L").resize((17, 16))
        pixels = list(image.getdata())
        result = 0
        for i in range(272):
            if i % 17 != 16:
                result = (result << 1) | (pixels[i] > pixels[i+1])
        return result

async def main():
    owned = json.loads(Path("tools/owned_nov26_public_images.json").read_text())
    engine, sessions = create_engine_and_session(get_settings().database_url)
    try:
        async with sessions() as session:
            ads = (await session.scalars(select(Apartment).where(
                Apartment.price.in_({p for p, _ in owned.values()}),
                Apartment.rooms.in_(("1","studio")), Apartment.telegram_chat_id == -1004389602150,
                Apartment.telegram_message_id.is_not(None),
                Apartment.lalafo_id.not_in([int(k) for k in owned]),
            ).order_by(Apartment.published_at.desc()))).all()
        groups = defaultdict(list)
        for ad in ads:
            if len(groups[ad.price]) < 80:
                groups[ad.price].append(ad)
        urls = {url for _, images in owned.values() for url in images}
        urls.update(url for rows in groups.values() for ad in rows for url in ad.photo_urls[:6])
        throttle = asyncio.Semaphore(24)
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            async def load(url):
                async with throttle:
                    try:
                        r = await client.get(url)
                        r.raise_for_status()
                        return url, digest(r.content)
                    except (httpx.HTTPError, OSError, ValueError):
                        return url, None
            photos = dict(await asyncio.gather(*(load(url) for url in urls)))
        print("VISUAL_COVERAGE=" + json.dumps([sum(v is not None for v in photos.values()),len(photos)]), flush=True)
        for aid, (price, images) in owned.items():
            refs = [photos[url] for url in images if photos.get(url) is not None]
            results = []
            for ad in groups[price]:
                candidates = [photos[url] for url in ad.photo_urls[:6] if photos.get(url) is not None]
                pairs = sorted(((a ^ b).bit_count(),i,j) for i,a in enumerate(refs) for j,b in enumerate(candidates))
                seen_a, seen_b = set(), set()
                scores = []
                for distance,i,j in pairs:
                    if distance <= 26 and i not in seen_a and j not in seen_b:
                        seen_a.add(i); seen_b.add(j); scores.append(distance)
                if len(scores) >= 2:
                    results.append({"source":ad.source_url,"message":ad.telegram_message_id,"scores":scores})
            print("VISUAL_MATCH=" + json.dumps({"owned":aid,"images":len(refs),"hits":results}), flush=True)
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())

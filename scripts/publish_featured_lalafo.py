from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.featured.posting import posting_payload
from app.lalafo.managed_ads import LalafoManagedAdsClient, publication_status
from app.models import Apartment, LalafoAutoReplyMeta
from scripts.scrape_publish import apartment_to_ad


logger = logging.getLogger(__name__)
MARKER_KEY = "requested_lalafo_filarmonia_35000"


def _extract_id(payload: dict) -> int | None:
    for key in ("id", "ad", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


async def _requested_apartment(sessions) -> Apartment:
    async with sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(Apartment)
                    .where(
                        Apartment.price == 35_000,
                        Apartment.district.ilike("%филармон%"),
                        Apartment.telegram_message_id.is_not(None),
                    )
                    .order_by(
                        Apartment.published_at.desc().nullslast(),
                        Apartment.id.desc(),
                    )
                    .limit(30)
                )
            ).all()
        )
    match = next((row for row in rows if len(row.photo_urls or []) >= 6), None)
    if match is None:
        raise LookupError(
            "Requested 35,000 som Filarmonia card with six photos was not found"
        )
    return match


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    await init_db(engine)
    try:
        async with sessions() as session:
            marker = await session.get(LalafoAutoReplyMeta, MARKER_KEY)
        if marker is not None:
            logger.info("Requested Lalafo ad already published: %s", marker.value)
            return 0

        apartment = await _requested_apartment(sessions)
        ad = apartment_to_ad(apartment)
        managed = LalafoManagedAdsClient(timeout=settings.http_timeout_seconds)
        try:
            login, password = settings.require_lalafo_auto_reply_credentials()
            await managed.login(login, password)
            draft = await managed.create_temp()
            draft_id = _extract_id(draft)
            if draft_id is None:
                raise RuntimeError("Lalafo draft response has no id")
            payload = posting_payload(ad)
            payload["id"] = draft_id
            await managed.update_temp(draft_id, payload)
            for photo_url in ad.photo_urls[:10]:
                await managed.upload_image(draft_id, photo_url)
            published = await managed.publish_temp(draft_id)
            ad_id = _extract_id(published)
            if ad_id is None:
                raise RuntimeError("Lalafo publication response has no id")
            details = await managed.my_ad_details(ad_id)
            status = publication_status(details)
            url = f"https://lalafo.kg/bishkek/ads/id-{ad_id}"
            async with sessions.begin() as session:
                session.add(
                    LalafoAutoReplyMeta(
                        key=MARKER_KEY,
                        value=json.dumps(
                            {
                                "ad_id": ad_id,
                                "url": url,
                                "status": status,
                                "source_apartment_id": apartment.id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
            logger.info(
                "Requested Lalafo ad published ad_id=%s status=%s url=%s source_apartment_id=%s",
                ad_id,
                status,
                url,
                apartment.id,
            )
            return 0
        finally:
            await managed.close()
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

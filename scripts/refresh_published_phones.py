from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoClient, LalafoError
from app.lalafo.models import PHONE_SOURCE_VERSION
from app.lalafo.parser import LalafoParseError
from app.models import Apartment
from app.state import ad_fingerprint


logger = logging.getLogger(__name__)


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    checked = 0
    changed = 0
    failed = 0
    try:
        async with sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(Apartment.id, Apartment.lalafo_id, Apartment.source_url).where(
                            Apartment.publication_status == "published",
                            Apartment.phone_source_version < PHONE_SOURCE_VERSION,
                        )
                    )
                ).all()
            )
        async with LalafoClient(
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        ) as client:
            for apartment_id, lalafo_id, source_url in rows:
                try:
                    ad = await client.detail(source_url)
                except (LalafoError, LalafoParseError, ValueError) as exc:
                    failed += 1
                    logger.warning(
                        "Could not verify phone apartment_id=%s lalafo_id=%s error=%s",
                        apartment_id,
                        lalafo_id,
                        type(exc).__name__,
                    )
                    continue
                if ad.lalafo_id != lalafo_id:
                    failed += 1
                    logger.warning(
                        "Refusing mismatched phone apartment_id=%s expected=%s received=%s",
                        apartment_id,
                        lalafo_id,
                        ad.lalafo_id,
                    )
                    continue
                async with sessions.begin() as session:
                    current_phone = await session.scalar(
                        select(Apartment.phone).where(Apartment.id == apartment_id)
                    )
                    await session.execute(
                        update(Apartment)
                        .where(Apartment.id == apartment_id)
                        .values(
                            phone=ad.phone,
                            fingerprint=ad_fingerprint(ad),
                            phone_source_version=PHONE_SOURCE_VERSION,
                        )
                    )
                checked += 1
                if current_phone != ad.phone:
                    changed += 1
                await asyncio.sleep(0.2)
        logger.info(
            "Published phones verified: checked=%d changed=%d failed=%d",
            checked,
            changed,
            failed,
        )
        return 1 if rows and checked == 0 else 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

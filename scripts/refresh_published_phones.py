from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
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
    disabled = 0
    unavailable = 0
    failed = 0
    try:
        async with sessions() as session:
            claimed_phones = set(
                (
                    await session.scalars(
                        select(Apartment.phone).where(
                            Apartment.publication_status == "published",
                            Apartment.active.is_(True),
                            Apartment.phone_source_version >= PHONE_SOURCE_VERSION,
                        )
                    )
                ).all()
            )
            rows = list(
                (
                    await session.execute(
                        select(
                            Apartment.id,
                            Apartment.lalafo_id,
                            Apartment.source_url,
                            Apartment.active,
                        )
                        .where(
                            Apartment.publication_status == "published",
                            Apartment.phone_source_version < PHONE_SOURCE_VERSION,
                        )
                        .order_by(Apartment.published_at.desc(), Apartment.id.desc())
                    )
                ).all()
            )
        async with LalafoClient(
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        ) as client:
            for apartment_id, lalafo_id, source_url, is_active in rows:
                try:
                    ad = await client.detail(source_url)
                except LalafoNotFound:
                    async with sessions.begin() as session:
                        await session.execute(
                            update(Apartment)
                            .where(Apartment.id == apartment_id)
                            .values(
                                phone_source_version=PHONE_SOURCE_VERSION,
                                active=False,
                            )
                        )
                    checked += 1
                    unavailable += 1
                    if is_active:
                        disabled += 1
                    logger.info(
                        "Disabled unavailable apartment_id=%s lalafo_id=%s",
                        apartment_id,
                        lalafo_id,
                    )
                    continue
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
                    should_disable = bool(is_active and ad.phone in claimed_phones)
                    await session.execute(
                        update(Apartment)
                        .where(Apartment.id == apartment_id)
                        .values(
                            phone=ad.phone,
                            fingerprint=ad_fingerprint(ad),
                            phone_source_version=PHONE_SOURCE_VERSION,
                            active=False if should_disable else is_active,
                        )
                    )
                checked += 1
                if current_phone != ad.phone:
                    changed += 1
                if should_disable:
                    disabled += 1
                elif is_active:
                    claimed_phones.add(ad.phone)
                await asyncio.sleep(0.2)
        logger.info(
            "Published phones verified: checked=%d changed=%d disabled=%d unavailable=%d failed=%d",
            checked,
            changed,
            disabled,
            unavailable,
            failed,
        )
        return 1 if rows and checked == 0 else 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

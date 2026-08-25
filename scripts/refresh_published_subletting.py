from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from sqlalchemy import select, update

from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError
from app.models import Apartment
from app.security import TokenSigner
from app.telegram.formatting import format_public_apartment
from app.telegram.keyboards import apartment_keyboard


logger = logging.getLogger(__name__)
REPAIR_LIMIT = 40


async def _edit_card(bot: Bot, apartment: Apartment, text: str, reply_markup) -> str:
    for attempt in range(5):
        try:
            await bot.edit_message_text(
                chat_id=apartment.telegram_chat_id,
                message_id=apartment.telegram_message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return "edited"
        except TelegramRetryAfter as exc:
            if attempt == 4:
                return "failed"
            await asyncio.sleep(max(float(exc.retry_after), 1.0) + 1.0)
        except TelegramNetworkError:
            if attempt == 4:
                return "failed"
            await asyncio.sleep(2**attempt)
        except TelegramBadRequest as exc:
            error_text = str(exc).casefold()
            if "message is not modified" in error_text:
                return "unchanged"
            if any(
                reason in error_text
                for reason in (
                    "message to edit not found",
                    "message can't be edited",
                    "chat not found",
                )
            ):
                return "skipped"
            logger.warning("Could not edit apartment card id=%s: %r", apartment.id, exc)
            return "failed"
    return "failed"


async def run() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(token=settings.require_bot_token())
    signer = TokenSigner(settings.require_callback_secret())
    checked = 0
    corrected = 0
    edited = 0
    unchanged = 0
    skipped = 0
    failed = 0
    try:
        async with sessions() as session:
            apartments = list(
                (
                    await session.scalars(
                        select(Apartment)
                        .where(
                            Apartment.publication_status == "published",
                            Apartment.active.is_(True),
                            Apartment.telegram_chat_id.is_not(None),
                            Apartment.telegram_message_id.is_not(None),
                        )
                        .order_by(Apartment.published_at.desc(), Apartment.id.desc())
                        .limit(REPAIR_LIMIT)
                    )
                ).all()
            )
        async with LalafoClient(
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        ) as client:
            for apartment in apartments:
                try:
                    ad = await client.detail(apartment.source_url)
                except LalafoNotFound:
                    skipped += 1
                    continue
                except (LalafoError, LalafoParseError, ValueError) as exc:
                    failed += 1
                    logger.warning(
                        "Could not verify subletting apartment_id=%s error=%s",
                        apartment.id,
                        type(exc).__name__,
                    )
                    continue
                checked += 1
                if apartment.no_subletting != ad.no_subletting:
                    corrected += 1
                async with sessions.begin() as session:
                    await session.execute(
                        update(Apartment)
                        .where(Apartment.id == apartment.id)
                        .values(no_subletting=ad.no_subletting)
                    )
                apartment.no_subletting = ad.no_subletting
                result = await _edit_card(
                    bot,
                    apartment,
                    format_public_apartment(
                        apartment,
                        bot_username=settings.telegram_bot_username,
                    ),
                    apartment_keyboard(
                        apartment.id,
                        signer=signer,
                        bot_username=settings.telegram_bot_username,
                        support_url=settings.support_url,
                    ),
                )
                if result == "edited":
                    edited += 1
                elif result == "unchanged":
                    unchanged += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
        logger.info(
            "Subletting cards repaired: checked=%d corrected=%d edited=%d "
            "unchanged=%d skipped=%d failed=%d",
            checked,
            corrected,
            edited,
            unchanged,
            skipped,
            failed,
        )
        return 1 if apartments and checked == 0 else 0
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.bot.main import BotRuntime
from app.matching.repository import MatchingRepository
from app.security import TokenSigner
from app.telegram.private_delivery import send_matching_card


logger = logging.getLogger(__name__)


async def run_matching_worker(runtime: BotRuntime) -> None:
    """Continuously deliver new matching apartments from the durable outbox."""
    matching: MatchingRepository = runtime.workflow_data["matching"]
    settings = runtime.workflow_data["settings"]
    signer: TokenSigner = runtime.workflow_data["signer"]
    while True:
        try:
            await matching.seed_notifications()
            for _ in range(100):
                claimed = await matching.claim_delivery()
                if claimed is None:
                    break
                try:
                    message_id = await send_matching_card(
                        runtime.bot,
                        user_id=claimed.user_id,
                        apartment=claimed.apartment,
                        signer=signer,
                        bot_username=settings.telegram_bot_username,
                    )
                except TelegramRetryAfter as exc:
                    await matching.finish_delivery(
                        claimed.delivery_id,
                        error="TelegramRetryAfter",
                        retry_after=float(exc.retry_after) + 1,
                    )
                except TelegramForbiddenError:
                    await matching.finish_delivery(
                        claimed.delivery_id,
                        error="TelegramForbiddenError",
                        disable_profile=True,
                    )
                except Exception as exc:
                    logger.exception(
                        "Personal apartment delivery %s failed", claimed.delivery_id
                    )
                    await matching.finish_delivery(
                        claimed.delivery_id,
                        error=type(exc).__name__,
                        retry_after=60,
                    )
                else:
                    await matching.finish_delivery(
                        claimed.delivery_id, message_id=message_id
                    )
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Personal matching worker recovered from a cycle failure")
            await asyncio.sleep(30)

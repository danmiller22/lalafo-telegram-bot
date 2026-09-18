from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.config import Settings
from app.lalafo.client import LalafoAccessError, LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from scripts.publish_inventory import _valid
from scripts.select_lalafo_proxy import find_working_proxies


router = Router(name="admin-lalafo-links")
logger = logging.getLogger(__name__)
_LALAFO_URL = re.compile(
    r"https://(?:www\.)?lalafo\.kg/[^\s<>]+-id-\d+(?:\?[^\s<>]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ").,;!?]}>\"'"
_publish_lock = asyncio.Lock()
REPOST_AFTER = timedelta(hours=48)
MAX_DISTRICT_LENGTH = 60
PROXY_DISCOVERY_TIMEOUT = 30.0
_manual_proxy_pool: list[str] = []


class ManualLalafoPublish(StatesGroup):
    waiting_for_district = State()


def extract_lalafo_url(text: str | None) -> str | None:
    match = _LALAFO_URL.search(text or "")
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) if match else None


def repost_available_at(
    published_at: datetime | None, *, now: datetime | None = None
) -> datetime | None:
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    available_at = published_at.astimezone(timezone.utc) + REPOST_AFTER
    current = now or datetime.now(timezone.utc)
    return available_at if available_at > current else None


def _is_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user is None:
        return False
    if settings.admin_user_id and user.id == settings.admin_user_id:
        return True
    return bool(
        settings.admin_username
        and getattr(user, "username", None)
        and user.username.casefold() == settings.admin_username.lstrip("@").casefold()
    )


def normalize_district(text: str | None) -> str | None:
    district = " ".join((text or "").split())
    if not district or len(district) > MAX_DISTRICT_LENGTH:
        return None
    return district


@router.message(ManualLalafoPublish.waiting_for_district, F.chat.type == "private", F.text)
async def receive_lalafo_district(
    message: Message,
    state: FSMContext,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    if not _is_admin(message, settings):
        await state.clear()
        return

    replacement_url = extract_lalafo_url(message.text)
    if replacement_url is not None:
        await state.update_data(source_url=replacement_url)
        await message.answer("Ссылка обновлена. Какой район написать в заголовке?")
        return

    if (message.text or "").strip().casefold() in {"отмена", "cancel"}:
        await state.clear()
        await message.answer("Публикация отменена.")
        return

    district = normalize_district(message.text)
    if district is None:
        await message.answer(
            f"Напишите район текстом — не больше {MAX_DISTRICT_LENGTH} символов."
        )
        return

    data = await state.get_data()
    url = extract_lalafo_url(data.get("source_url"))
    await state.clear()
    if url is None:
        await message.answer("Ссылка потерялась. Пришлите объявление Lalafo ещё раз.")
        return

    await _publish_lalafo_url(
        message,
        url=url,
        district=district,
        settings=settings,
        apartments=apartments,
        signer=signer,
        bot=bot,
    )


@router.message(F.chat.type == "private", F.text.regexp(_LALAFO_URL))
async def request_lalafo_district(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    url = extract_lalafo_url(message.text)
    if url is None or not _is_admin(message, settings):
        return
    await state.set_state(ManualLalafoPublish.waiting_for_district)
    await state.update_data(source_url=url)
    await message.answer("Какой район написать в заголовке карточки?")


async def _publish_lalafo_url(
    message: Message,
    *,
    url: str,
    district: str,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:

    async with _publish_lock:
        await message.answer("⏳ Проверяю квартиру…")
        global _manual_proxy_pool
        proxy_url = settings.lalafo_proxy_url.strip() or ",".join(_manual_proxy_pool)
        if not proxy_url:
            try:
                selected = await asyncio.wait_for(
                    find_working_proxies(), timeout=PROXY_DISCOVERY_TIMEOUT
                )
            except Exception:
                logger.exception("Could not discover a Lalafo proxy for manual link")
                selected = []
            proxy_url = ",".join(selected)
            if proxy_url:
                _manual_proxy_pool = selected
                logger.info(
                    "Using %d discovered Lalafo proxy route(s) for manual link",
                    len(selected),
                )
            else:
                logger.warning("No Lalafo proxy discovered; trying direct connection")
        try:
            ad = None
            for load_attempt in range(2):
                try:
                    async with LalafoClient(
                        timeout=settings.http_timeout_seconds,
                        max_retries=settings.http_max_retries,
                        proxy_url=proxy_url,
                    ) as client:
                        ad = await client.detail(url)
                    break
                except LalafoAccessError:
                    if load_attempt:
                        raise
                    logger.warning("Lalafo access denied; refreshing manual proxy pool")
                    try:
                        selected = await asyncio.wait_for(
                            find_working_proxies(), timeout=PROXY_DISCOVERY_TIMEOUT
                        )
                    except Exception:
                        logger.exception("Could not refresh Lalafo proxy pool")
                        selected = []
                    _manual_proxy_pool = selected
                    proxy_url = ",".join(selected)
            if ad is None:
                raise LalafoAccessError("Lalafo access was not bypassed")
        except LalafoNotFound:
            await message.answer("❌ Объявление удалено или больше недоступно.")
            return
        except (LalafoError, LalafoParseError, ValueError) as exc:
            logger.exception("Admin Lalafo link could not be loaded")
            await message.answer(f"⚠️ Не удалось загрузить Lalafo: {exc}")
            return

        ad = ad.model_copy(update={"district": district})

        valid, reason = _valid(ad, settings)
        if not valid:
            await message.answer(f"❌ Квартира не прошла фильтр: {reason}.")
            return

        # Manual admin submissions are explicit overrides: publish immediately
        # even when the automatic parser has already seen the same apartment.
        apartment = await apartments.upsert_discovered(ad, discovery_priority=True)
        publisher = TelegramPublisher(
            bot,
            chat_id=settings.telegram_group_id,
            signer=signer,
            bot_username=settings.telegram_bot_username,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )
        try:
            published = await publisher.publish(apartment.id, ad)
        except TelegramPublishError:
            logger.exception("Admin Lalafo card could not be published")
            await message.answer("⚠️ Telegram не принял карточку. Попробуйте ещё раз.")
            return

        # Telegram has already accepted the public post. Never ask the admin to
        # retry just because recording its metadata failed: that would create a
        # duplicate card in the group.
        try:
            await apartments.mark_published(
                apartment.id,
                chat_id=settings.telegram_group_id,
                message_id=published.message_id,
            )
        except Exception:
            logger.exception("Published Lalafo card metadata could not be saved")
            await message.answer(
                "✅ Карточка опубликована, но отметка в базе не сохранилась. "
                "Повторно ссылку сейчас не отправляйте."
            )
            return

        await message.answer(
            f"✅ Квартира опубликована в группе. ID: {ad.lalafo_id}."
        )

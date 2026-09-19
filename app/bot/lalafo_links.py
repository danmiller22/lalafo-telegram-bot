from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import re
from urllib.parse import parse_qs, urlsplit
import httpx

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.lalafo.client import LalafoAccessError, LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError
from app.lalafo.models import LalafoAd
from app.lalafo.phone import normalize_kg_phone
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublishError, TelegramPublisher
from scripts.publish_inventory import _valid
from scripts.select_lalafo_proxy import find_working_proxies


router = Router(name="admin-lalafo-links")
owner_router = Router(name="owner-manual-card")
main_router = Router(name="main-admin-lalafo-links")
logger = logging.getLogger(__name__)
_LALAFO_URL = re.compile(
    r"https://(?:www\.)?lalafo\.kg/[^\s<>]+-id-\d+(?:\?[^\s<>]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ").,;!?]}>\"'"
_publish_lock = asyncio.Lock()
_forward_batch_lock = asyncio.Lock()
REPOST_AFTER = timedelta(hours=48)
MAX_DISTRICT_LENGTH = 60
PROXY_DISCOVERY_TIMEOUT = 30.0
FORWARD_BATCH_DELAY_SECONDS = 1.5
_manual_proxy_pool: list[str] = []


@dataclass
class _ForwardBatch:
    messages: list[Message] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


_forward_batches: dict[tuple[int, int, int], _ForwardBatch] = {}


class ManualLalafoPublish(StatesGroup):
    waiting_for_district = State()


class ManualCardPublish(StatesGroup):
    waiting_for_photos = State()
    waiting_for_phone = State()
    waiting_for_rooms = State()
    waiting_for_district = State()
    waiting_for_price = State()


def extract_lalafo_url(text: str | None) -> str | None:
    match = _LALAFO_URL.search(text or "")
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) if match else None


def has_lalafo_url(message: Message) -> bool:
    """Match a Lalafo link anywhere in the admin's message text."""
    return extract_lalafo_url(message.text) is not None


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


def _is_owner(message: Message, settings: Settings) -> bool:
    """Strict owner check for manual card creation and duplication."""
    user = message.from_user
    if user is None:
        return False
    if settings.admin_user_id and user.id == settings.admin_user_id:
        return True
    return bool(
        settings.admin_username
        and user.username
        and user.username.casefold() == settings.admin_username.lstrip("@").casefold()
    )


def normalize_district(text: str | None) -> str | None:
    district = " ".join((text or "").split())
    if not district or len(district) > MAX_DISTRICT_LENGTH:
        return None
    return district


def _forwarded_card_fields(text: str | None) -> tuple[str, str, int] | None:
    value = text or ""
    room_match = re.search(r"🏠\s*([12])-комнатная\s+квартира", value, re.IGNORECASE)
    district_match = re.search(r"^📍\s*(.+?)\s*$", value, re.MULTILINE)
    price_match = re.search(r"^💰\s*([\d\s]+)\s*сом\s*$", value, re.MULTILINE)
    if not room_match or not district_match or not price_match:
        return None
    district = normalize_district(district_match.group(1))
    if district is None:
        return None
    price = int(re.sub(r"\D", "", price_match.group(1)))
    return room_match.group(1), district, price


def _forwarded_apartment_id(message: Message, signer: TokenSigner) -> int | None:
    markup = message.reply_markup
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            callback = button.callback_data or ""
            if callback.startswith("dup:"):
                apartment_id = signer.verify_id("duplicate", callback.removeprefix("dup:"))
                if apartment_id is not None:
                    return apartment_id
            if callback.startswith("view:"):
                apartment_id = signer.verify_id("view", callback.removeprefix("view:"))
                if apartment_id is not None:
                    return apartment_id
            if button.url:
                start_token = parse_qs(urlsplit(button.url).query).get("startapp", [None])[0]
                if start_token:
                    apartment_id = signer.decode_public_start_id(start_token)
                    if apartment_id is not None:
                        return apartment_id
    return None


async def _resolve_forwarded_apartment(
    messages: list[Message],
    *,
    apartments: ApartmentRepository,
    signer: TokenSigner,
):
    for message in messages:
        apartment_id = _forwarded_apartment_id(message, signer)
        if apartment_id is not None:
            apartment = await apartments.get(apartment_id)
            if apartment is not None:
                return apartment

    for message in messages:
        origin = message.forward_origin
        origin_message_id = getattr(origin, "message_id", None)
        origin_chat_id = getattr(getattr(origin, "chat", None), "id", None)
        if origin_message_id is None or origin_chat_id is None:
            continue
        apartment = await apartments.get_by_telegram_message(
            chat_id=origin_chat_id,
            message_id=origin_message_id,
        )
        if apartment is not None:
            return apartment

    text_message = next(
        (message for message in messages if _forwarded_card_fields(message.text or message.caption)),
        None,
    )
    if text_message is None:
        return None
    fields = _forwarded_card_fields(text_message.text or text_message.caption)
    assert fields is not None
    rooms, district, price = fields
    origin_date = getattr(text_message.forward_origin, "date", None)
    return await apartments.find_forwarded_card(
        rooms=rooms,
        district=district,
        price=price,
        origin_date=origin_date,
    )


async def _publish_forwarded_batch(
    messages: list[Message],
    *,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    reply_to = messages[-1]
    apartment = await _resolve_forwarded_apartment(
        messages,
        apartments=apartments,
        signer=signer,
    )
    if apartment is None or not apartment.photo_urls:
        await reply_to.answer(
            "⚠️ Не удалось распознать исходную карточку. "
            "Перешлите вместе фотографии и текст карточки одним действием."
        )
        return
    publisher = TelegramPublisher(
        bot,
        chat_id=settings.telegram_group_id,
        signer=signer,
        bot_username=settings.telegram_bot_username,
        support_url=settings.support_bot_url,
        max_photos=settings.max_photos_per_apartment,
    )
    try:
        published = await publisher.publish(apartment.id, apartment)
        await apartments.mark_published(
            apartment.id,
            chat_id=settings.telegram_group_id,
            message_id=published.message_id,
        )
    except Exception:
        logger.exception("Could not rebuild admin-forwarded Telegram card")
        await reply_to.answer("⚠️ Не удалось опубликовать карточку. Попробуйте ещё раз.")
        return
    await reply_to.answer("✅ Карточка опубликована альбомом с рабочими кнопками.")


async def _flush_forward_batch(
    key: tuple[int, int, int],
    *,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    try:
        await asyncio.sleep(FORWARD_BATCH_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    async with _forward_batch_lock:
        batch = _forward_batches.pop(key, None)
    if batch is None:
        return
    await _publish_forwarded_batch(
        batch.messages,
        settings=settings,
        apartments=apartments,
        signer=signer,
        bot=bot,
    )


@owner_router.message(F.chat.type == "private", F.forward_origin)
async def duplicate_forwarded_card(
    message: Message,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    """Buffer a complete forwarded card and rebuild it from stored source data."""
    # Forward-to-group is intentionally stricter than the legacy link flow:
    # only the configured numeric owner ID may trigger it.
    if not _is_owner(message, settings):
        return
    key = (id(bot), message.chat.id, message.from_user.id)
    async with _forward_batch_lock:
        batch = _forward_batches.setdefault(key, _ForwardBatch())
        batch.messages.append(message)
        if batch.task is not None and not batch.task.done():
            batch.task.cancel()
        batch.task = asyncio.create_task(
            _flush_forward_batch(
                key,
                settings=settings,
                apartments=apartments,
                signer=signer,
                bot=bot,
            )
        )


@owner_router.message(Command("addcard"), F.chat.type == "private")
async def start_manual_card(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        return
    await state.clear()
    await state.set_state(ManualCardPublish.waiting_for_photos)
    await state.update_data(photo_urls=[])
    await message.answer(
        "Пришлите фотографии квартиры (можно несколько сообщений), "
        "затем отправьте «готово»."
    )


@owner_router.callback_query(F.data == "manual:add")
async def start_manual_card_button(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if (
        callback.message is None
        or not (
            (settings.admin_user_id and callback.from_user.id == settings.admin_user_id)
            or (
                settings.admin_username
                and callback.from_user.username
                and callback.from_user.username.casefold()
                == settings.admin_username.lstrip("@").casefold()
            )
        )
    ):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await state.set_state(ManualCardPublish.waiting_for_photos)
    await state.update_data(photo_urls=[])
    await callback.message.answer(
        "Пришлите фотографии квартиры (можно несколько сообщений), "
        "затем отправьте «готово»."
    )
    await callback.answer()


@owner_router.message(ManualCardPublish.waiting_for_photos, F.chat.type == "private", F.photo)
async def manual_card_photo(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    data = await state.get_data()
    photos = list(data.get("photo_urls") or [])
    if len(photos) >= 10:
        await message.answer("Достаточно 10 фото. Напишите «готово».")
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photo_urls=photos)
    await message.answer(f"Фото добавлено: {len(photos)}. Ещё фото или «готово».")


@owner_router.message(ManualCardPublish.waiting_for_photos, F.chat.type == "private", F.text)
async def manual_card_photos_done(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    if (message.text or "").strip().casefold() not in {"готово", "готов", "done"}:
        await message.answer("Пришлите фото или напишите «готово».")
        return
    data = await state.get_data()
    if len(data.get("photo_urls") or []) < 2:
        await message.answer("Нужно минимум 2 фотографии.")
        return
    await state.set_state(ManualCardPublish.waiting_for_phone)
    await message.answer("Введите номер хозяина, например +996 700 123 456.")


@owner_router.message(ManualCardPublish.waiting_for_phone, F.chat.type == "private", F.text)
async def manual_card_phone(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    try:
        phone = normalize_kg_phone(message.text)
    except ValueError:
        await message.answer("Номер не распознан. Введите кыргызский номер ещё раз.")
        return
    await state.update_data(phone=phone)
    await state.set_state(ManualCardPublish.waiting_for_rooms)
    await message.answer("Сколько комнат? Напишите 1 или 2.")


@owner_router.message(ManualCardPublish.waiting_for_rooms, F.chat.type == "private", F.text)
async def manual_card_rooms(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    value = (message.text or "").strip().casefold()
    rooms = {
        "1": "1",
        "одна": "1",
        "однокомнатная": "1",
        "2": "2",
        "две": "2",
        "двухкомнатная": "2",
    }.get(value)
    if rooms is None:
        await message.answer("Напишите только 1 или 2 комнаты.")
        return
    await state.update_data(rooms=rooms)
    await state.set_state(ManualCardPublish.waiting_for_district)
    await message.answer("Какой район указать в карточке?")


@owner_router.message(ManualCardPublish.waiting_for_district, F.chat.type == "private", F.text)
async def manual_card_district(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    district = normalize_district(message.text)
    if district is None:
        await message.answer("Укажите район одним коротким сообщением (до 60 символов).")
        return
    await state.update_data(district=district)
    await state.set_state(ManualCardPublish.waiting_for_price)
    await message.answer("Какая цена в сомах? Например: 28000")


@owner_router.message(ManualCardPublish.waiting_for_price, F.chat.type == "private", F.text)
async def manual_card_price(
    message: Message,
    state: FSMContext,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    if not _is_owner(message, settings):
        await state.clear()
        return
    digits = re.sub(r"\D", "", message.text or "")
    try:
        price = int(digits)
    except ValueError:
        price = 0
    if not 20_000 <= price <= 40_000:
        await message.answer("Укажите цену от 20 000 до 40 000 сом.")
        return
    data = await state.get_data()
    await state.clear()
    # Negative IDs are reserved for bot-created cards and cannot collide with
    # numeric Lalafo advertisements.
    lalafo_id = -int(datetime.now(timezone.utc).timestamp() * 1000)
    ad = LalafoAd(
        lalafo_id=lalafo_id,
        source_url=f"manual://telegram/{abs(lalafo_id)}",
        phone=data["phone"],
        price=price,
        currency="KGS",
        rooms=data["rooms"],
        district=data["district"],
        city="Бишкек",
        photo_urls=list(data["photo_urls"]),
        category_id=2044,
        no_subletting=True,
        owner_listing=True,
        source_title="Ручное объявление",
    )
    await message.answer("⏳ Публикую карточку в группу…")
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
        await apartments.mark_published(
            apartment.id,
            chat_id=settings.telegram_group_id,
            message_id=published.message_id,
        )
    except Exception:
        logger.exception("Could not publish manually created apartment")
        await message.answer("⚠️ Не удалось опубликовать карточку. Проверьте группу.")
        return
    await message.answer("✅ Карточка опубликована в группе с кнопками оплаты.")


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


@router.message(F.chat.type == "private", has_lalafo_url)
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


# Aiogram does not allow one Router instance to be attached to two
# dispatchers. Register the same flow on a separate router so the main Arenda
# bot remains a working fallback when the dedicated link-bot token is revoked.
main_router.message.register(
    receive_lalafo_district,
    ManualLalafoPublish.waiting_for_district,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    duplicate_forwarded_card,
    F.chat.type == "private",
    F.forward_origin,
)
main_router.message.register(
    start_manual_card,
    Command("addcard"),
    F.chat.type == "private",
)
main_router.callback_query.register(start_manual_card_button, F.data == "manual:add")
main_router.message.register(
    manual_card_photo,
    ManualCardPublish.waiting_for_photos,
    F.chat.type == "private",
    F.photo,
)
main_router.message.register(
    manual_card_photos_done,
    ManualCardPublish.waiting_for_photos,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    manual_card_phone,
    ManualCardPublish.waiting_for_phone,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    manual_card_rooms,
    ManualCardPublish.waiting_for_rooms,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    manual_card_district,
    ManualCardPublish.waiting_for_district,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    manual_card_price,
    ManualCardPublish.waiting_for_price,
    F.chat.type == "private",
    F.text,
)
main_router.message.register(
    request_lalafo_district,
    F.chat.type == "private",
    has_lalafo_url,
)


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

        if settings.lalafo_relay_url.strip():
            try:
                async with httpx.AsyncClient(timeout=45.0) as relay_client:
                    response = await relay_client.post(
                        settings.lalafo_relay_url.rstrip("/") + "/internal/lalafo/publish",
                        headers={"X-Lalafo-Relay-Secret": settings.lalafo_relay_secret},
                        json={"ad": ad.model_dump(mode="json"), "district": district},
                    )
                    response.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                logger.exception("Main Arenda bot relay failed")
                await message.answer(f"⚠️ Основной бот не принял карточку: {exc}")
                return
            await message.answer(f"✅ Передал карточку основному боту. ID: {ad.lalafo_id}.")
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

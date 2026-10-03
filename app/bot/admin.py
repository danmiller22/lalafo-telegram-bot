from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import DUPLICATE_PREFIX
from app.config import Settings
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.private_delivery import send_private_public_card
from app.wanted.repository import WantedAdRepository

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _is_admin(user_id: int, settings: Settings) -> bool:
    return bool(settings.admin_user_id and user_id == settings.admin_user_id)


@router.callback_query(F.data.startswith(DUPLICATE_PREFIX))
async def duplicate_apartment_callback(
    callback: CallbackQuery,
    settings: Settings,
    apartments: ApartmentRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    """Send an original apartment card to the admin's private bot chat.

    This deliberately uploads fresh photos and text instead of forwarding the
    old Telegram message. The callback is signed and rejected for every
    non-admin user, so the control is harmless when visible in a public card.
    """
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    token = (callback.data or "").removeprefix(DUPLICATE_PREFIX)
    apartment_id = signer.verify_id("duplicate", token)
    if apartment_id is None:
        await callback.answer("Недействительная подпись.", show_alert=True)
        return
    apartment = await apartments.get(apartment_id)
    if apartment is None or not apartment.photo_urls:
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    try:
        await send_private_public_card(
            bot,
            user_id=callback.from_user.id,
            apartment=apartment,
            signer=signer,
            bot_username=settings.telegram_bot_username,
            support_url=settings.support_bot_url,
        )
    except Exception:
        logger.exception("Could not duplicate apartment %s", apartment.id)
        await callback.answer("Не удалось опубликовать карточку.", show_alert=True)
        return
    await callback.answer("✅ Оригинальная карточка отправлена вам в личный чат бота.")


@router.message(Command("admin"))
async def admin_handler(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    await message.answer("Панель администратора: /pending /stats")


@router.message(Command("pending"))
async def pending_handler(
    message: Message,
    settings: Settings,
    wanted_ads: WantedAdRepository,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    wanted_rows = await wanted_ads.pending()
    if not wanted_rows:
        await message.answer("Нет ожидающих проверок.")
        return
    lines = ["⏳ Ожидают публикации:"]
    lines.extend(
        f"Заявка #{row.id} · {row.district} · "
        f"@{row.username or row.telegram_user_id}"
        for row in wanted_rows
    )
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def stats_handler(
    message: Message,
    settings: Settings,
    apartments: ApartmentRepository,
    wanted_ads: WantedAdRepository,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    wanted_counts = await wanted_ads.counts()
    await message.answer(
        "\n".join(
            [
                f"Опубликовано квартир: {await apartments.published_count()}",
                f"Wanted ads pending: {wanted_counts.get('pending', 0)}",
                f"Wanted ads published: {wanted_counts.get('published', 0)}",
            ]
        )
    )

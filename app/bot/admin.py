from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import DUPLICATE_PREFIX
from app.config import Settings
from app.payments.repository import ApartmentRepository
from app.payments.repository import PaymentRepository
from app.payments.service import PaymentService
from app.payments.review import review_keyboard, review_text
from app.telegram.private_delivery import send_private_contact
from app.security import TokenSigner
from app.telegram.private_delivery import send_private_public_card
from app.wanted.repository import WantedAdRepository

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _is_admin(user_id: int, settings: Settings) -> bool:
    return bool(settings.admin_user_id and user_id == settings.admin_user_id)


@router.callback_query(F.data.startswith("payment:"))
async def payment_decision_handler(callback: CallbackQuery, settings: Settings,
                                   service: PaymentService, signer: TokenSigner,
                                   bot: Bot) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"yes", "no"}:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    values = signer.verify_values(f"payment-{parts[1]}", parts[2], count=2)
    if values is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    request_id, generation = values
    outcome = await service.decide(request_id, approve=parts[1] == "yes",
                                   actor_id=callback.from_user.id, generation=generation)
    await callback.answer("Доступ выдан." if outcome == "approved" else
                          "Доступ не выдан." if outcome == "rejected" else "Заявка уже обработана.")
    if outcome not in {"approved", "rejected"}:
        return
    request = await service.get_request(request_id)
    if request is None:
        return
    if callback.message:
        try:
            await callback.message.edit_text(review_text(request) +
                                             ("\nДоступ выдан." if outcome == "approved" else "\nОтказано."),
                                             parse_mode="HTML")
        except Exception:
            logger.exception("Could not update payment review message %s", request_id)
    try:
        if outcome == "approved" and request.apartment:
            await send_private_contact(bot, user_id=request.telegram_user_id,
                                       apartment=request.apartment,
                                       support_url=settings.support_bot_url,
                                       max_photos=settings.max_photos_per_apartment)
        else:
            await bot.send_message(request.telegram_user_id, "Оплата не подтверждена. Доступ не выдан.")
    except Exception:
        logger.exception("Could not deliver payment decision %s; Mini App still shows the decision", request_id)


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
    payments: PaymentRepository,
    signer: TokenSigner,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    wanted_rows = await wanted_ads.pending()
    payment_rows = await payments.pending()
    for row in payment_rows:
        await message.answer(review_text(row), reply_markup=review_keyboard(row, signer), parse_mode="HTML")
    if not wanted_rows and not payment_rows:
        await message.answer("Нет ожидающих проверок.")
        return
    if not wanted_rows:
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

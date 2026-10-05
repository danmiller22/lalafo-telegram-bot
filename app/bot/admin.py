from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import DUPLICATE_PREFIX
from app.config import Settings
from app.payments.repository import ApartmentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.telegram.private_delivery import send_private_contact, send_private_public_card
from app.wanted.repository import WantedAdRepository
from app.matching.repository import MatchingRepository

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
    matching: MatchingRepository,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    wanted_counts = await wanted_ads.counts()
    funnel = await matching.stats(days=7)
    await message.answer(
        "\n".join(
            [
                f"Опубликовано квартир: {await apartments.published_count()}",
                f"Wanted ads pending: {wanted_counts.get('pending', 0)}",
                f"Wanted ads published: {wanted_counts.get('published', 0)}",
                "",
                "Подбор за 7 дней:",
                f"Переходы: {funnel.get('start', 0)}",
                f"Из Lalafo: {funnel.get('lalafo:start', 0)}",
                f"Горячие карточки: {funnel.get('hot_cards_shown', 0)}",
                f"Сохранённые фильтры: {funnel.get('filter_saved', 0)}",
                f"Открытия карточек: {funnel.get('card_opened', 0)}",
                f"Открытия оплаты: {funnel.get('payment_opened', 0)}",
                f"Выдачи доступа: {funnel.get('access_granted', 0)}",
                f"Активные профили: {funnel.get('profiles', 0)}",
                f"Автоуведомления: {funnel.get('notifications', 0)}",
            ]
        )
    )


@router.callback_query(F.data.startswith("payment:"))
async def payment_review_callback(
    callback: CallbackQuery,
    settings: Settings,
    service: PaymentService,
    signer: TokenSigner,
    bot: Bot,
    matching: MatchingRepository | None = None,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"a", "r"} or callback.message is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    approve = parts[1] == "a"
    request_id = signer.verify_id("payment-approve" if approve else "payment-reject", parts[2])
    if request_id is None:
        await callback.answer("Недействительная подпись.", show_alert=True)
        return
    outcome = await service.decide(
        request_id,
        approve=approve,
        actor_id=callback.from_user.id,
        expected_admin_message_id=callback.message.message_id,
    )
    if outcome not in {"approved", "rejected"}:
        await callback.answer("Эта заявка уже обработана или кнопка устарела.", show_alert=True)
        return
    await callback.answer("Доступ выдан." if approve else "В доступе отказано.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Заявка #{request_id}: {'доступ выдан' if approve else 'отказано'}.")
    except Exception:
        logger.exception("Could not update payment review message")
    request = await service.get_request(request_id)
    if request is None:
        return
    if approve and matching is not None:
        try:
            profile = await matching.profile(request.telegram_user_id)
            await matching.event(
                request.telegram_user_id, "access_granted",
                source=profile.source if profile is not None else "telegram",
                apartment_id=request.apartment_id,
            )
        except Exception:
            logger.exception("Could not record approved payment in funnel")
    try:
        if approve:
            await bot.send_message(request.telegram_user_id, "Оплата подтверждена. Доступ к контактам открыт.")
            if request.apartment:
                await send_private_contact(bot, user_id=request.telegram_user_id,
                    apartment=request.apartment, support_url=settings.support_bot_url,
                    max_photos=settings.max_photos_per_apartment)
        else:
            await bot.send_message(request.telegram_user_id,
                "Оплата не подтверждена. Проверьте перевод или обратитесь в поддержку.")
    except Exception:
        logger.exception("Could not deliver payment decision to customer %s", request.telegram_user_id)

from __future__ import annotations

import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import ADMIN_PREFIX
from app.config import Settings
from app.payments.repository import ApartmentRepository, PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.telegram.formatting import format_admin_decision, user_label
from app.telegram.keyboards import private_payment_keyboard
from app.telegram.private_delivery import send_private_contact
from app.payment_plans import WEEK_PLAN
from app.wanted.repository import WantedAdRepository

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _is_admin(user_id: int, settings: Settings) -> bool:
    return bool(settings.admin_user_id and user_id == settings.admin_user_id)


@router.message(Command("admin"))
async def admin_handler(message: Message, settings: Settings) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    await message.answer("Панель администратора: /pending /stats")


@router.message(Command("pending"))
async def pending_handler(
    message: Message,
    settings: Settings,
    payments: PaymentRepository,
    wanted_ads: WantedAdRepository,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    rows = await payments.pending()
    wanted_rows = await wanted_ads.pending()
    if not rows and not wanted_rows:
        await message.answer("Нет ожидающих проверок.")
        return
    lines = ["⏳ Ожидают проверки:"]
    lines.extend(f"#{row.id} · квартира #{row.apartment_id} · {user_label(row)}" for row in rows)
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
    payments: PaymentRepository,
    apartments: ApartmentRepository,
    wanted_ads: WantedAdRepository,
) -> None:
    if not _is_admin(message.from_user.id, settings):
        return
    counts = await payments.counts()
    wanted_counts = await wanted_ads.counts()
    await message.answer(
        "\n".join(
            [
                f"Опубликовано квартир: {await apartments.published_count()}",
                f"Pending payments: {counts['pending']}",
                f"Approved payments: {counts['approved']}",
                f"Rejected payments: {counts['rejected']}",
                f"Wanted ads pending: {wanted_counts.get('pending', 0)}",
                f"Wanted ads published: {wanted_counts.get('published', 0)}",
            ]
        )
    )


@router.callback_query(F.data.startswith(ADMIN_PREFIX))
async def admin_callback(
    callback: CallbackQuery,
    settings: Settings,
    service: PaymentService,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"a", "r"}:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    request_id = signer.verify_id("admin", parts[2])
    if request_id is None:
        await callback.answer("Недействительная подпись.", show_alert=True)
        return
    approve = parts[1] == "a"
    outcome = await service.decide(request_id, approve=approve, actor_id=callback.from_user.id)
    if outcome == "forbidden":
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    request = await service.get_request(request_id)
    if request is None:
        await callback.answer("Запрос не найден.", show_alert=True)
        return
    if outcome not in {"approved", "rejected"}:
        await callback.answer("Запрос уже обработан.", show_alert=True)
        return
    decision_text = format_admin_decision(request, outcome == "approved")
    if callback.message.photo or callback.message.document:
        await callback.message.edit_caption(caption=decision_text)
    else:
        await callback.message.edit_text(decision_text)
    apartment = request.apartment
    if outcome == "approved":
        try:
            if request.plan == WEEK_PLAN and request.access_expires_at:
                expires = request.access_expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                local_expiry = expires.astimezone(ZoneInfo("Asia/Bishkek"))
                await bot.send_message(
                    request.telegram_user_id,
                    "⭐ Доступ ко всем номерам активирован на 7 дней.\n"
                    f"Работает до {local_expiry:%d.%m.%Y %H:%M} по Бишкеку.\n\n"
                    "Нажимайте «Посмотреть номер» под любой квартирой — бот "
                    "сразу пришлёт её полную карточку сюда.",
                )
            await send_private_contact(
                bot,
                user_id=request.telegram_user_id,
                apartment=apartment,
                support_url=settings.support_url,
                max_photos=settings.max_photos_per_apartment,
            )
        except Exception:
            logger.exception("Could not deliver approved apartment privately")
            await callback.answer(
                "Оплата подтверждена, но Telegram не принял личное сообщение. "
                "Клиент получит карточку при повторном открытии бота.",
                show_alert=True,
            )
            return
        await callback.answer("✅ Подтверждено. Доступ выдан клиенту.")
    else:
        try:
            await bot.send_message(
                request.telegram_user_id,
                "❌ Оплата пока не подтверждена. Проверьте платеж или повторите попытку.",
                reply_markup=private_payment_keyboard(
                    apartment.id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                ),
            )
        except Exception:
            logger.exception("Could not notify rejected buyer privately")
        await callback.answer("Оплата отклонена. Клиенту отправлено уведомление.")

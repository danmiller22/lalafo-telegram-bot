from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import CONTACT_PREFIX, PAID_PREFIX
from app.config import Settings
from app.lalafo.phone import display_phone
from app.payments.repository import PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.telegram.formatting import format_admin_card
from app.telegram.keyboards import admin_keyboard, payment_keyboard

logger = logging.getLogger(__name__)
router = Router(name="user")


def _start_payload(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


@router.message(CommandStart())
async def start_handler(
    message: Message,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
) -> None:
    payload = _start_payload(message)
    if payload.startswith("pay_"):
        apartment_id = signer.verify_id("payment-link", payload[4:])
        if apartment_id is None:
            await message.answer("Ссылка недействительна. Вернитесь к карточке квартиры.")
            return
        result = await service.contact_status(message.from_user.id, apartment_id)
        if result.status == "approved":
            await message.answer("✅ Оплата подтверждена. Нажмите «Получить номер» под квартирой.")
            return
        if result.status == "pending":
            await message.answer("⏳ Оплата уже отправлена на проверку.")
            return
        if result.status == "unavailable":
            await message.answer("Квартира больше недоступна.")
            return
        text = "❌ Оплата не подтверждена.\nМожно отправить на проверку повторно." if result.status == "rejected" else "Для получения номера оплатите через Finik."
        await message.answer(
            text,
            reply_markup=payment_keyboard(
                apartment_id, signer=signer, payment_url=settings.finik_payment_url
            ),
        )
        return
    await message.answer(
        "Бот открывает контакты квартир после ручной проверки оплаты.\n"
        "Выберите квартиру в группе и нажмите «Получить номер»."
    )


@router.message(Command("myid"))
async def myid_handler(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("status"))
async def status_handler(message: Message) -> None:
    await message.answer("✅ Бот работает")


@router.callback_query(F.data.startswith(CONTACT_PREFIX))
async def contact_handler(
    callback: CallbackQuery,
    service: PaymentService,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    token = (callback.data or "")[len(CONTACT_PREFIX) :]
    apartment_id = signer.verify_id("contact", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status == "approved" and result.apartment:
        text = f"✅ Оплата подтверждена\n\n📞 Номер собственника:\n{display_phone(result.apartment.phone)}"
        await callback.answer(text[:200], show_alert=True, cache_time=0)
        return
    if result.status == "pending":
        await callback.answer("⏳ Оплата уже отправлена на проверку.", show_alert=True)
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    me = await bot.get_me()
    deep_token = signer.sign_id("payment-link", apartment_id)
    url = f"https://t.me/{me.username}?start=pay_{deep_token}"
    text = (
        "❌ Оплата не подтверждена. Можно отправить повторно."
        if result.status == "rejected"
        else "Для получения номера оплатите через Finik."
    )
    await callback.answer(text, show_alert=True, url=url, cache_time=0)


@router.callback_query(F.data.startswith(PAID_PREFIX))
async def paid_handler(
    callback: CallbackQuery,
    service: PaymentService,
    payments: PaymentRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    token = (callback.data or "")[len(PAID_PREFIX) :]
    apartment_id = signer.verify_id("paid", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    try:
        submission = await service.submit_payment(
            user_id=callback.from_user.id,
            apartment_id=apartment_id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
    except LookupError:
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    if submission.outcome == "approved":
        await callback.answer(
            "✅ Оплата подтверждена. Нажмите «Получить номер» под квартирой.",
            show_alert=True,
        )
        return
    request = await payments.get_request(submission.request.id)
    if request is None:
        await callback.answer("Не удалось загрузить запрос.", show_alert=True)
        return
    if submission.outcome == "pending" and request.admin_message_id:
        await callback.answer("⏳ Оплата уже отправлена на проверку.", show_alert=True)
        return
    if not settings.admin_user_id:
        logger.error("ADMIN_USER_ID is not configured; payment request remains pending")
        await callback.answer(
            "⏳ Запрос сохранён. Администратор ещё не настроен — обратитесь в поддержку.",
            show_alert=True,
        )
        return
    if not await payments.claim_admin_notification(request.id):
        await callback.answer("⏳ Оплата уже отправлена на проверку.", show_alert=True)
        return
    try:
        admin_message = await bot.send_message(
            settings.admin_user_id,
            format_admin_card(request),
            reply_markup=admin_keyboard(request.id, signer=signer),
        )
    except Exception as exc:
        await payments.release_admin_notification(request.id)
        logger.error("Admin payment notification failed: %s", type(exc).__name__)
        await callback.answer(
            "⏳ Запрос сохранён. Не удалось уведомить администратора; попробуйте ещё раз.",
            show_alert=True,
        )
        return
    await payments.finish_admin_notification(request.id, admin_message.message_id)
    await callback.answer("⏳ Оплата отправлена на проверку.", show_alert=True)

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import CONTACT_PREFIX, PAID_PREFIX, VIEW_PREFIX
from app.config import Settings
from app.lalafo.phone import display_phone
from app.payments.repository import PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.telegram.formatting import format_admin_card
from app.telegram.keyboards import (
    admin_keyboard,
    finik_keyboard,
    payment_keyboard,
    status_keyboard,
)

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
        text = (
            "❌ Оплата не подтверждена. Оплатите повторно через Finik."
            if result.status == "rejected"
            else "Для получения номера оплатите через Finik."
        )
        payment_message = await message.answer(text)
        redirect_token = signer.sign_values(
            "finik-redirect", apartment_id, message.chat.id, payment_message.message_id
        )
        redirect_url = f"{settings.require_public_base_url()}/pay/{redirect_token}"
        await payment_message.edit_reply_markup(reply_markup=finik_keyboard(redirect_url))
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
    payments: PaymentRepository,
    settings: Settings,
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
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
            )
        )
    await callback.answer("Оплатите через Finik, затем нажмите «Я оплатил».")


async def _submit_for_review(
    callback: CallbackQuery,
    apartment_id: int,
    *,
    service: PaymentService,
    payments: PaymentRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    """Submit one payment check from either the group or legacy private flow."""
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
            "✅ Оплата подтверждена. Нажмите «Получить номер» ещё раз.",
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
            "⏳ Запрос сохранён. Администратор увидит его в списке ожидающих.",
            show_alert=True,
        )
        return
    await payments.finish_admin_notification(request.id, admin_message.message_id)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=status_keyboard(apartment_id, signer=signer)
            )
        except Exception:
            logger.exception("Could not switch apartment card to payment status button")
    await callback.answer(
        "⏳ Оплата отправлена на проверку.\n\n"
        "Не закрывайте эту карточку: после подтверждения бот уведомит вас "
        "прямо под квартирой.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith(VIEW_PREFIX))
async def view_contact_handler(
    callback: CallbackQuery,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
) -> None:
    token = (callback.data or "")[len(VIEW_PREFIX) :]
    apartment_id = signer.verify_id("view", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status == "approved" and result.apartment:
        text = (
            "✅ Оплата подтверждена\n\n"
            f"📞 Номер собственника:\n{display_phone(result.apartment.phone)}"
        )
        await callback.answer(text[:200], show_alert=True, cache_time=0)
        return
    if result.status == "pending":
        await callback.answer(
            "⏳ Оплата ещё проверяется. Мы уведомим вас под этой квартирой.",
            show_alert=True,
        )
        return
    if result.status == "rejected":
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                )
            )
        await callback.answer(
            "❌ Оплата не подтверждена. Можно повторить оплату и отправить её снова.",
            show_alert=True,
        )
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await callback.answer(
        "Сначала откройте ссылку на оплату и нажмите «Я оплатил».",
        show_alert=True,
    )


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
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status == "approved" and result.apartment:
        text = (
            "✅ Оплата подтверждена\n\n"
            f"📞 Номер собственника:\n{display_phone(result.apartment.phone)}"
        )
        await callback.answer(text[:200], show_alert=True, cache_time=0)
        return
    if result.status == "pending":
        await callback.answer("⏳ Оплата ещё проверяется.", show_alert=True)
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await _submit_for_review(
        callback,
        apartment_id,
        service=service,
        payments=payments,
        signer=signer,
        settings=settings,
        bot=bot,
    )

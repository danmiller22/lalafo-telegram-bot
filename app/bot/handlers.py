from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import CONTACT_PREFIX, PAID_PREFIX, VIEW_PREFIX
from app.config import Settings
from app.payments.repository import PaymentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.telegram.formatting import format_admin_card, format_apartment
from app.telegram.keyboards import (
    admin_keyboard,
    apartment_keyboard,
    payment_keyboard,
    private_payment_keyboard,
    status_keyboard,
)
from app.telegram.private_delivery import send_private_contact
from app.wanted.keyboards import main_menu_keyboard

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
    bot: Bot,
    state: FSMContext,
) -> None:
    await state.clear()
    payload = _start_payload(message)
    if payload.startswith("pay_"):
        apartment_id = signer.verify_start_id("payment-link", payload[4:])
        if apartment_id is None:
            await message.answer("Ссылка недействительна. Вернитесь к карточке квартиры.")
            return
        result = await service.contact_status(message.from_user.id, apartment_id)
        if result.status == "approved" and result.apartment:
            await send_private_contact(
                bot,
                user_id=message.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_url,
            )
            return
        if result.status == "unavailable":
            await message.answer("Квартира больше недоступна.")
            return
        apartment_text = format_apartment(result.apartment) if result.apartment else "Квартира"
        if result.status == "pending":
            text = (
                "⏳ Оплата уже отправлена на проверку.\n\n"
                f"{apartment_text}\n\n"
                "Повторно оплачивать не нужно. После подтверждения полная карточка "
                "с номером придёт сюда автоматически."
            )
        elif result.status == "rejected":
            text = (
                "❌ Оплата не подтверждена.\n\n"
                f"{apartment_text}\n\n"
                "Доступ к номеру собственника стоит 100 сом. "
                "Можно повторить оплату и снова проверить её."
            )
        else:
            text = (
                "🔐 Доступ к номеру собственника\n\n"
                f"{apartment_text}\n\n"
                "Стоимость одного номера — 100 сом.\n"
                "После оплаты нажмите «Проверить оплату»."
            )
        await message.answer(
            text,
            reply_markup=private_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
                support_url=settings.support_url,
                pending=result.status == "pending",
            ),
        )
        return
    await message.answer(
        "🏠 Сервис аренды квартир\n\n"
        "Здесь можно получить контакт собственника из группы или разместить "
        "собственную заявку «Ищу квартиру».",
        reply_markup=main_menu_keyboard(settings.support_url),
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
        await callback.answer(
            "✅ Оплата подтверждена. Откройте личный чат бота из обновлённой кнопки.",
            show_alert=True,
        )
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=apartment_keyboard(
                    apartment_id,
                    signer=signer,
                    bot_username=settings.telegram_bot_username,
                    support_url=settings.support_url,
                )
            )
        return
    if result.status == "pending":
        await callback.answer("⏳ Оплата уже отправлена на проверку.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_url,
                    )
                )
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    if callback.message:
        await callback.answer("Откройте обновлённую кнопку под квартирой.", show_alert=True)
        await callback.message.edit_reply_markup(
            reply_markup=apartment_keyboard(
                apartment_id,
                signer=signer,
                bot_username=settings.telegram_bot_username,
                support_url=settings.support_url,
            )
        )
        return
    await callback.answer("Открываю оплату…")


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
        result = await service.contact_status(callback.from_user.id, apartment_id)
        if (
            callback.message
            and callback.message.chat.type == "private"
            and result.apartment
        ):
            await send_private_contact(
                bot,
                user_id=callback.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_url,
            )
            await callback.answer("✅ Полная карточка отправлена вам в этот чат.")
        else:
            await callback.answer(
                "✅ Оплата подтверждена. Откройте личный чат бота из карточки квартиры.",
                show_alert=True,
            )
        return
    is_private = bool(callback.message and callback.message.chat.type == "private")
    if submission.outcome == "pending":
        alert_text = (
            "⏳ Оплата уже проверяется.\n\n"
            "Повторно оплачивать не нужно. Полная карточка с номером придёт сюда "
            "после подтверждения."
            if is_private
            else "⏳ Оплата уже проверяется.\n\n"
            "Кнопки останутся под квартирой — повторно оплачивать не нужно."
        )
    else:
        alert_text = (
            "⏳ Оплата отправлена на проверку.\n\n"
            "После подтверждения бот автоматически пришлёт сюда полную карточку с номером."
            if is_private
            else "⏳ Оплата отправлена на проверку.\n\n"
            "После подтверждения откройте личный чат бота из карточки квартиры."
        )
    await callback.answer(alert_text, show_alert=True)
    if callback.message:
        try:
            if callback.message.chat.type == "private":
                reply_markup = private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                    pending=True,
                )
            else:
                reply_markup = status_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                )
            await callback.message.edit_reply_markup(reply_markup=reply_markup)
        except Exception:
            logger.exception("Could not keep payment status keyboard on apartment card")
    request = await payments.get_request(submission.request.id)
    if request is None:
        logger.error("Could not reload payment request id=%s", submission.request.id)
        return
    if submission.outcome == "pending" and request.admin_message_id:
        return
    if not settings.admin_user_id:
        logger.error("ADMIN_USER_ID is not configured; payment request remains pending")
        return
    if not await payments.claim_admin_notification(request.id):
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
        return
    await payments.finish_admin_notification(request.id, admin_message.message_id)


@router.callback_query(F.data.startswith(VIEW_PREFIX))
async def view_contact_handler(
    callback: CallbackQuery,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    token = (callback.data or "")[len(VIEW_PREFIX) :]
    apartment_id = signer.verify_id("view", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status == "approved" and result.apartment:
        if callback.message and callback.message.chat.type == "private":
            await send_private_contact(
                bot,
                user_id=callback.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_url,
            )
            await callback.answer("✅ Полная карточка отправлена вам в этот чат.")
        else:
            await callback.answer(
                "✅ Оплата подтверждена. Откройте личный чат бота из карточки квартиры.",
                show_alert=True,
            )
        return
    if result.status == "pending":
        await callback.answer(
            "⏳ Оплата ещё проверяется.\n\n"
            "Кнопка останется на месте. После подтверждения нажмите её ещё раз.",
            show_alert=True,
        )
        if callback.message:
            try:
                if callback.message.chat.type == "private":
                    reply_markup = private_payment_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_url,
                        pending=True,
                    )
                else:
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_url,
                    )
                await callback.message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
        return
    if result.status == "rejected":
        await callback.answer(
            "❌ Оплата не подтверждена. Можно повторить оплату и отправить её снова.",
            show_alert=True,
        )
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                )
            )
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await callback.answer(
        "Сначала откройте ссылку на оплату и нажмите «Я оплатил».",
        show_alert=True,
    )
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_url,
                )
            )
        except Exception:
            logger.exception("Could not restore unpaid payment keyboard")


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
        if callback.message and callback.message.chat.type == "private":
            await send_private_contact(
                bot,
                user_id=callback.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_url,
            )
            await callback.answer("✅ Полная карточка отправлена вам в этот чат.")
        else:
            await callback.answer(
                "✅ Оплата подтверждена. Откройте личный чат бота из карточки квартиры.",
                show_alert=True,
            )
        return
    if result.status == "pending":
        await callback.answer(
            "⏳ Оплата ещё проверяется. Повторно оплачивать не нужно.",
            show_alert=True,
        )
        if callback.message:
            try:
                if callback.message.chat.type == "private":
                    reply_markup = private_payment_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_url,
                        pending=True,
                    )
                else:
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_url,
                    )
                await callback.message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
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

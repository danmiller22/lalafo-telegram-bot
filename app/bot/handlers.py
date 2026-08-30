from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import CONTACT_PREFIX, PAID_PREFIX, PLAN_PREFIX, VIEW_PREFIX
from app.config import Settings
from app.payments.repository import PaymentRepository
from app.payment_plans import WEEK_PLAN, WEEK_PRICE, plan_label
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.support.handlers import begin_support
from app.telegram.formatting import format_admin_card, format_apartment
from app.telegram.keyboards import (
    admin_keyboard,
    apartment_keyboard,
    payment_keyboard,
    pending_payment_keyboard,
    private_payment_keyboard,
    receipt_payment_keyboard,
    status_keyboard,
)
from app.telegram.private_delivery import send_private_contact
from app.wanted.keyboards import main_menu_keyboard
from app.wanted.handlers import begin_wanted_form

logger = logging.getLogger(__name__)
router = Router(name="user")


def _start_payload(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _show_main_menu(message: Message, settings: Settings) -> None:
    await message.answer(
        "🏠 Сервис аренды квартир\n\n"
        "Здесь можно получить контакт собственника из группы или разместить "
        "собственную заявку «Ищу квартиру».",
        reply_markup=main_menu_keyboard(settings.support_bot_url),
    )


@router.message(CommandStart())
async def start_handler(
    message: Message,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
    state: FSMContext,
) -> None:
    payload = _start_payload(message)
    if payload == "want":
        await begin_wanted_form(message, state)
        return
    if payload == "support":
        await begin_support(message, state)
        return
    if not payload:
        # A plain /start is the most common customer action.  Send the menu
        # before doing even lightweight session cleanup so the visible response
        # is never held behind unrelated state work.
        await _show_main_menu(message, settings)
        await state.clear()
        return
    await state.clear()
    if payload.startswith("pay_"):
        payment_token = payload[4:]
        apartment_id = signer.verify_start_id("payment-link", payment_token)
        if apartment_id is None:
            apartment_id = signer.decode_public_start_id(payment_token)
        if apartment_id is None:
            await _show_main_menu(message, settings)
            return
        result = await service.contact_status(message.from_user.id, apartment_id)
        if result.status == "approved" and result.apartment:
            await send_private_contact(
                bot,
                user_id=message.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment,
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
        elif result.status == "awaiting_receipt":
            text = (
                "🧾 Жду чек об оплате.\n\n"
                f"{apartment_text}\n\n"
                "Оплатите недельный доступ и отправьте сюда фото или файл чека."
            )
        elif result.status == "rejected":
            text = (
                "❌ Оплата не подтверждена.\n\n"
                f"{apartment_text}\n\n"
                "Оформите недельный доступ, оплатите и отправьте новый чек."
            )
        else:
            text = (
                "🔐 Доступ к номерам собственников\n\n"
                f"{apartment_text}\n\n"
                f"Все номера на 7 дней — {WEEK_PRICE} сом.\n"
                "Оформите недельный доступ ниже."
            )
        reply_markup = (
            receipt_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
                support_url=settings.support_bot_url,
            )
            if result.status == "awaiting_receipt"
            else pending_payment_keyboard(
                apartment_id,
                signer=signer,
                support_url=settings.support_bot_url,
            )
            if result.status == "pending"
            else private_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
                support_url=settings.support_bot_url,
                pending=result.status == "pending",
            )
        )
        await message.answer(text, reply_markup=reply_markup)
        return
    await _show_main_menu(message, settings)


@router.callback_query(F.data.startswith(PLAN_PREFIX))
async def plan_handler(
    callback: CallbackQuery,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] != "w":
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    plan = WEEK_PLAN
    purpose = "plan-week"
    apartment_id = signer.verify_id(purpose, parts[2])
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    access = await service.contact_status(callback.from_user.id, apartment_id)
    if access.status == "approved" and access.apartment:
        await send_private_contact(
            bot,
            user_id=callback.from_user.id,
            apartment=access.apartment,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )
        await callback.answer("✅ Карточка с номером отправлена вам.")
        return
    if access.status == "pending":
        await callback.answer("⏳ Ваш чек уже проверяется.", show_alert=True)
        return
    try:
        submission = await service.begin_payment(
            user_id=callback.from_user.id,
            apartment_id=apartment_id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            plan=plan,
        )
    except LookupError:
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    if submission.outcome == "approved":
        await callback.answer("✅ Этот номер уже доступен вам.", show_alert=True)
        return
    text = (
        f"💳 {plan_label(plan)} — {WEEK_PRICE} сом\n\n"
        "1. Откройте ссылку на оплату.\n"
        "2. Оплатите указанную сумму.\n"
        "3. Отправьте в этот чат фото или файл чека.\n\n"
        "Без чека заявка на проверку не отправляется."
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=receipt_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
                support_url=settings.support_bot_url,
            ),
        )


@router.message(F.chat.type == "private", F.photo | F.document)
async def receipt_handler(
    message: Message,
    service: PaymentService,
    payments: PaymentRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        return
    request = await service.submit_receipt(
        user_id=message.from_user.id,
        file_id=file_id,
        file_type=file_type,
    )
    if request is None:
        await message.answer(
            "Сначала откройте нужную квартиру, нажмите «Посмотреть номер» "
            "и оформите недельный доступ."
        )
        return
    await message.answer(
        "✅ Чек получен и отправлен на проверку.\n\n"
        "Пожалуйста, подождите. После подтверждения бот сразу пришлёт карточку с номером."
    )
    if not settings.admin_user_id or not await payments.claim_admin_notification(request.id):
        return
    try:
        caption = format_admin_card(request)
        markup = admin_keyboard(request.id, signer=signer)
        if file_type == "photo":
            admin_message = await bot.send_photo(
                settings.admin_user_id,
                file_id,
                caption=caption,
                reply_markup=markup,
            )
        else:
            admin_message = await bot.send_document(
                settings.admin_user_id,
                file_id,
                caption=caption,
                reply_markup=markup,
            )
    except Exception as exc:
        await payments.release_admin_notification(request.id)
        logger.error("Admin receipt notification failed: %s", type(exc).__name__)
        return
    await payments.finish_admin_notification(request.id, admin_message.message_id)


@router.callback_query(F.data == "receipt:send")
async def receipt_prompt_handler(callback: CallbackQuery) -> None:
    await callback.answer(
        "🧾 Отправьте в этот чат фото или файл чека. После этого оплата "
        "автоматически уйдёт администратору на проверку.",
        show_alert=True,
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
                    support_url=settings.support_bot_url,
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
                        support_url=settings.support_bot_url,
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
                support_url=settings.support_bot_url,
            )
        )
        return
    await callback.answer("Открываю оплату…")


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
                support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment,
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
                    reply_markup = pending_payment_keyboard(
                        apartment_id,
                        signer=signer,
                        support_url=settings.support_bot_url,
                    )
                else:
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_bot_url,
                    )
                await callback.message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
        return
    if result.status == "awaiting_receipt":
        await callback.answer(
            "🧾 Отправьте фото или файл чека в этот чат.", show_alert=True
        )
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=receipt_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                )
            )
        return
    if result.status == "rejected":
        await callback.answer(
            "❌ Оплата не подтверждена. Можно повторить оплату и отправить её снова.",
            show_alert=True,
        )
        if callback.message:
            reply_markup = (
                private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                )
                if callback.message.chat.type == "private"
                else apartment_keyboard(
                    apartment_id,
                    signer=signer,
                    bot_username=settings.telegram_bot_username,
                    support_url=settings.support_bot_url,
                )
            )
            await callback.message.edit_reply_markup(reply_markup=reply_markup)
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await callback.answer(
        "Оформите недельный доступ и после оплаты отправьте чек боту.",
        show_alert=True,
    )
    if callback.message:
        try:
            reply_markup = (
                private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                )
                if callback.message.chat.type == "private"
                else apartment_keyboard(
                    apartment_id,
                    signer=signer,
                    bot_username=settings.telegram_bot_username,
                    support_url=settings.support_bot_url,
                )
            )
            await callback.message.edit_reply_markup(reply_markup=reply_markup)
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
                support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment,
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
                    reply_markup = pending_payment_keyboard(
                        apartment_id,
                        signer=signer,
                        support_url=settings.support_bot_url,
                    )
                else:
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=settings.finik_payment_url,
                        support_url=settings.support_bot_url,
                    )
                await callback.message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
        return
    if result.status == "awaiting_receipt":
        await callback.answer(
            "🧾 После оплаты отправьте фото или файл чека в этот чат.",
            show_alert=True,
        )
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=receipt_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                )
            )
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await callback.answer(
        "Оформите недельный доступ, оплатите и отправьте чек в этот чат.",
        show_alert=True,
    )
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                )
            )
        except Exception:
            logger.exception("Could not replace legacy payment keyboard")

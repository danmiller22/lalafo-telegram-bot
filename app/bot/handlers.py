from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import (
    CONTACT_PREFIX,
    PAID_PREFIX,
    PLAN_PREFIX,
    VIEW_PREFIX,
)
from app.config import Settings
from app.availability import AvailabilityService
from app.payments.repository import PaymentRepository
from app.payment_plans import (
    WEEK_PLAN,
    plan_label,
    plan_price,
)
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.support.handlers import begin_support
from app.telegram.formatting import format_apartment
from app.telegram.keyboards import (
    apartment_keyboard,
    payment_keyboard,
    private_payment_keyboard,
    status_keyboard,
)
from app.telegram.private_delivery import send_private_contact
from app.wanted.keyboards import main_menu_keyboard
from app.wanted.handlers import begin_wanted_form
from app.terms import PRIVACY_TEXT, TermsConsentRepository

logger = logging.getLogger(__name__)
router = Router(name="user")


class ReceiptUpload(StatesGroup):
    waiting = State()


def _payment_details(plan: str | None, settings: Settings, *, signer: TokenSigner | None = None, apartment_id: int | None = None) -> tuple[str, int]:
    url = settings.weekly_finik_payment_url
    if not url and signer is not None and apartment_id is not None:
        token = signer.sign_start_id("miniapp-apartment", apartment_id)
        url = f"https://t.me/{settings.telegram_bot_username.lstrip('@')}/access?startapp={token}"
    return url, plan_price(WEEK_PLAN)


def _start_payload(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _show_main_menu(message: Message, settings: Settings) -> None:
    await message.answer(
        "🏠 Сервис аренды квартир\n\n"
        "Здесь можно получить контакт по объявлению из группы или разместить "
        "собственную заявку «Ищу квартиру».",
        reply_markup=main_menu_keyboard(
            settings.support_bot_url,
            include_admin=bool(
                message.from_user
                and settings.admin_user_id
                and message.from_user.id == settings.admin_user_id
            ),
        ),
    )


@router.message(
    F.chat.type == "private",
    F.text.func(
        lambda text: (text or "").strip().casefold() in {"/старт", "старт"}
    ),
)
@router.message(CommandStart())
async def start_handler(
    message: Message,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
    state: FSMContext,
    terms_consents: TermsConsentRepository | None = None,
) -> None:
    payload = _start_payload(message)
    if payload == "want":
        await begin_wanted_form(message, state)
        return
    if payload == "support":
        await begin_support(message, state)
        return
    if payload == "privacy":
        await state.clear()
        await message.answer(PRIVACY_TEXT)
        return
    if not payload:
        # A plain /start is the most common customer action.  Send the menu
        # before doing even lightweight session cleanup so the visible response
        # is never held behind unrelated state work.
        await _show_main_menu(message, settings)
        await state.clear()
        return
    await state.clear()
    if payload.startswith("receipt_"):
        apartment_id = signer.verify_start_id("receipt", payload[8:])
        if apartment_id is None:
            await message.answer("Откройте загрузку чека из окна оплаты.")
            return
        result = await service.contact_status(message.from_user.id, apartment_id)
        if result.status == "approved" and result.apartment:
            await send_private_contact(bot, user_id=message.from_user.id,
                apartment=result.apartment, support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment)
            return
        if result.status not in {"awaiting_receipt", "pending"}:
            await message.answer("Сначала откройте оплату кнопкой «Получить номер».")
            return
        await state.set_state(ReceiptUpload.waiting)
        await state.update_data(receipt_apartment_id=apartment_id)
        await message.answer("Отправьте чек об оплате: фото или PDF-файл.")
        return
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
                "💳 Оплата\n\n"
                f"{apartment_text}\n\n"
                "Нажмите «Получить номер»."
            )
        elif result.status == "awaiting_receipt":
            text = (
                "💳 Оплата\n\n"
                f"{apartment_text}\n\n"
                "После оплаты нажмите «Получить номер»."
            )
        elif result.status == "rejected":
            text = (
                "💳 Откройте оплату повторно.\n\n"
                f"{apartment_text}\n\n"
                "Доступ к контактам на 7 дней — 500 сом."
            )
        else:
            text = "Доступ к контактам на 7 дней — 500 сом."
        reply_markup = private_payment_keyboard(
            apartment_id, signer=signer,
            payment_url=settings.weekly_finik_payment_url,
            support_url=settings.support_bot_url,
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
    terms_consents: TermsConsentRepository | None = None,
) -> None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] != "w":
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    plan = WEEK_PLAN
    purpose = "plan-week"
    payment_url, price = _payment_details(plan, settings)
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
    if not payment_url:
        try:
            from app.web import _finik_checkout_url
            payment_url = await _finik_checkout_url(
                settings, service.payments, submission.request,
                apartment_id=apartment_id, plan=plan,
            )
        except Exception:
            logger.exception("Could not create weekly Finik checkout")
            await callback.answer("Не удалось открыть оплату. Попробуйте ещё раз.", show_alert=True)
            return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "💳 Оплата\n\n"
            f"Тариф: {plan_label(plan)}\n"
            f"Сумма: {price} сом\n\n"
            "Нажмите кнопку ниже — откроется оплата Finik.",
            reply_markup=payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=payment_url,
                support_url=settings.support_bot_url,
                price=price,
            ),
        )


@router.callback_query(F.data == "menu:status")
async def status_button_handler(callback: CallbackQuery) -> None:
    await callback.answer("✅ Бот работает", show_alert=True)


@router.callback_query(F.data == "menu:privacy")
async def privacy_button_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(PRIVACY_TEXT)


@router.callback_query(F.data.startswith("terms:accept:"))
async def terms_accept_handler(
    callback: CallbackQuery,
    signer: TokenSigner,
    settings: Settings,
    terms_consents: TermsConsentRepository,
) -> None:
    token = (callback.data or "").removeprefix("terms:accept:")
    apartment_id = signer.verify_id("terms", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    await terms_consents.accept(callback.from_user.id)
    await callback.answer("Условия приняты.")
    if callback.message:
        await callback.message.edit_text(
            "Доступ к контактам на 7 дней — 500 сом.",
            reply_markup=private_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.weekly_finik_payment_url,
                support_url=settings.support_bot_url,
                monthly_payment_url=settings.monthly_finik_payment_url,
            ),
        )


@router.callback_query(F.data.startswith("availability:"))
async def availability_handler(
    callback: CallbackQuery,
    signer: TokenSigner,
    availability: AvailabilityService,
) -> None:
    token = (callback.data or "").removeprefix("availability:")
    apartment_id = signer.verify_id("availability", token)
    if apartment_id is None:
        apartment_id = signer.decode_public_id(token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    try:
        result = await availability.check(apartment_id)
    except Exception:
        logger.exception("Availability callback failed")
        await callback.answer("Не удалось проверить, попробуйте позже.", show_alert=True)
        return
    await callback.answer(result.message, show_alert=True)


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
            "✅ Доступ активен. Откройте личный чат бота из обновлённой кнопки.",
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
        payment_url, price = _payment_details(result.plan, settings, signer=signer, apartment_id=apartment_id)
        await callback.answer("Нажмите «Получить номер».", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=payment_url,
                        support_url=settings.support_bot_url,
                        price=price,
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
                "✅ Доступ активен. Откройте личный чат бота из карточки квартиры.",
                show_alert=True,
            )
        return
    if result.status == "pending":
        payment_url, price = _payment_details(result.plan, settings, signer=signer, apartment_id=apartment_id)
        await callback.answer(
            "После оплаты нажмите «Получить номер».",
            show_alert=True,
        )
        if callback.message:
            try:
                if callback.message.chat.type == "private":
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=payment_url,
                        support_url=settings.support_bot_url,
                        price=price,
                    )
                else:
                    reply_markup = status_keyboard(
                        apartment_id,
                        signer=signer,
                        payment_url=payment_url,
                        support_url=settings.support_bot_url,
                        price=price,
                    )
                await callback.message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logger.exception("Could not restore pending payment keyboard")
        return
    if result.status == "awaiting_receipt":
        payment_url, price = _payment_details(result.plan, settings, signer=signer, apartment_id=apartment_id)
        await callback.answer("После оплаты нажмите «Получить номер».", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=payment_url,
                    support_url=settings.support_bot_url,
                    price=price,
                )
            )
        return
    if result.status == "rejected":
        await callback.answer(
            "Откройте оплату повторно.",
            show_alert=True,
        )
        if callback.message:
            reply_markup = (
                private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.weekly_finik_payment_url,
                    support_url=settings.support_bot_url,
                    monthly_payment_url=settings.monthly_finik_payment_url,
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
        "Оплатите доступ и нажмите «Получить номер».",
        show_alert=True,
    )
    if callback.message:
        try:
            reply_markup = (
                private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.weekly_finik_payment_url,
                    support_url=settings.support_bot_url,
                    monthly_payment_url=settings.monthly_finik_payment_url,
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
        await send_private_contact(
            bot,
            user_id=callback.from_user.id,
            apartment=result.apartment,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )
        await callback.answer("✅ Карточка с номером отправлена вам.")
        return
    if result.status in {"awaiting_receipt", "pending"}:
        request = await payments.mark_payment_claimed(
            user_id=callback.from_user.id,
            apartment_id=apartment_id,
        )
        if request is not None:
            result = await service.contact_status(callback.from_user.id, apartment_id)
        if result.status == "approved" and result.apartment:
            await send_private_contact(
                bot,
                user_id=callback.from_user.id,
                apartment=result.apartment,
                support_url=settings.support_bot_url,
                max_photos=settings.max_photos_per_apartment,
            )
            await callback.answer("✅ Карточка с номером отправлена вам.")
            return
        await callback.answer("Не удалось выдать доступ. Попробуйте ещё раз.", show_alert=True)
        return
    if result.status == "unavailable":
        await callback.answer("Квартира больше недоступна.", show_alert=True)
        return
    await callback.answer(
        "Сначала выберите тариф и откройте оплату.",
        show_alert=True,
    )
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.weekly_finik_payment_url,
                    support_url=settings.support_bot_url,
                    monthly_payment_url=settings.monthly_finik_payment_url,
                )
            )
        except Exception:
            logger.exception("Could not replace legacy payment keyboard")


@router.message(StateFilter(None, ReceiptUpload.waiting), F.chat.type == "private", F.photo | F.document)
async def receipt_handler(message: Message, payments: PaymentRepository,
                          service: PaymentService, settings: Settings,
                          bot: Bot, state: FSMContext) -> None:
    document = message.document
    if document and document.mime_type not in {"application/pdf", "image/jpeg", "image/png", "image/webp"}:
        await message.answer("Отправьте чек фотографией или PDF-файлом.")
        return
    data = await state.get_data()
    if await payments.active_weekly_access(message.from_user.id) is not None:
        await state.clear()
        apartment_id = data.get("receipt_apartment_id")
        if apartment_id is not None:
            result = await service.contact_status(message.from_user.id, apartment_id)
            if result.status == "approved" and result.apartment:
                await send_private_contact(bot, user_id=message.from_user.id,
                    apartment=result.apartment, support_url=settings.support_bot_url,
                    max_photos=settings.max_photos_per_apartment)
                return
        await message.answer("Доступ к контактам уже активен. Нажмите «Получить номер» под квартирой.")
        return
    file_id = message.photo[-1].file_id if message.photo else document.file_id
    request = await payments.submit_receipt(
        user_id=message.from_user.id, file_id=file_id,
        file_type="photo" if message.photo else "document",
        apartment_id=data.get("receipt_apartment_id"))
    if request is None:
        await message.answer("Откройте оплату кнопкой «Получить номер».")
        return
    await state.clear()
    await message.answer("Чек принят.")
    result = await service.contact_status(message.from_user.id, request.apartment_id)
    if result.status == "approved" and result.apartment:
        await send_private_contact(bot, user_id=message.from_user.id,
            apartment=result.apartment, support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment)


@router.message(StateFilter(ReceiptUpload.waiting), F.chat.type == "private")
async def receipt_file_prompt(message: Message) -> None:
    await message.answer("Отправьте чек фотографией или PDF-файлом.")

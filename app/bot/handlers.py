from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
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
    MONTH_PLAN,
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
from app.terms import PRIVACY_TEXT, TERMS_TEXT, TermsConsentRepository

logger = logging.getLogger(__name__)
router = Router(name="user")


class ReceiptUpload(StatesGroup):
    waiting = State()


def _payment_details(plan: str | None, settings: Settings) -> tuple[str, int]:
    if plan == MONTH_PLAN:
        return settings.monthly_finik_payment_url, plan_price(MONTH_PLAN)
    return settings.finik_payment_url, plan_price(WEEK_PLAN)


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
    if payload == "terms":
        await message.answer(TERMS_TEXT)
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
                "Загрузите чек оплаты.\n\n"
                f"{apartment_text}"
            )
        elif result.status == "awaiting_receipt":
            text = (
                "💳 Оплата\n\n"
                f"{apartment_text}\n\n"
                "После оплаты загрузите чек."
            )
        elif result.status == "rejected":
            text = (
                "💳 Откройте оплату повторно.\n\n"
                f"{apartment_text}\n\n"
                "Выберите тариф ниже."
            )
        else:
            text = "Выберите тариф."
        reply_markup = (
            payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=_payment_details(result.plan, settings)[0],
                support_url=settings.support_bot_url,
                price=_payment_details(result.plan, settings)[1],
            )
            if result.status == "awaiting_receipt"
            else status_keyboard(
                apartment_id,
                signer=signer,
                payment_url=_payment_details(result.plan, settings)[0],
                support_url=settings.support_bot_url,
                price=_payment_details(result.plan, settings)[1],
            )
            if result.status == "pending"
            else private_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
                support_url=settings.support_bot_url,
                pending=result.status == "pending",
                monthly_payment_url=settings.monthly_finik_payment_url,
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
    terms_consents: TermsConsentRepository | None = None,
) -> None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"w", "m"}:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    plan = WEEK_PLAN if parts[1] == "w" else MONTH_PLAN
    purpose = "plan-week" if plan == WEEK_PLAN else "plan-month"
    payment_url, price = _payment_details(plan, settings)
    if not payment_url:
        await callback.answer(
            "Тариф на 30 дней подключается. Выберите 7 дней.", show_alert=True
        )
        return
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
        await callback.answer("Загрузите чек оплаты.", show_alert=True)
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
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "💳 Оплата\n\n"
            f"Тариф: {plan_label(plan)}\n"
            f"Сумма: {price} сом",
            reply_markup=payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=payment_url,
                support_url=settings.support_bot_url,
                price=price,
            ),
        )


@router.callback_query(F.data == "paylocked")
async def payment_locked_handler(callback: CallbackQuery) -> None:
    await callback.answer("Примите пользовательское соглашение.", show_alert=True)


@router.callback_query(F.data.startswith("payuncheck:"))
async def payment_uncheck_handler(callback: CallbackQuery, signer: TokenSigner,
                                  settings: Settings) -> None:
    parts = (callback.data or "").split(":", 2)
    apartment_id = signer.verify_id(f"pay-consent-{parts[1]}", parts[2]) if len(parts) == 3 and parts[1] in {"w", "m"} else None
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    payment_url, price = _payment_details(WEEK_PLAN if parts[1] == "w" else MONTH_PLAN, settings)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=payment_keyboard(
            apartment_id, signer=signer, payment_url=payment_url,
            support_url=settings.support_bot_url, price=price))


@router.callback_query(F.data.startswith("payconsent:"))
async def payment_consent_handler(callback: CallbackQuery, signer: TokenSigner,
                                  settings: Settings, terms_consents: TermsConsentRepository,
                                  service: PaymentService) -> None:
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"w", "m"}:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    apartment_id = signer.verify_id(f"pay-consent-{parts[1]}", parts[2])
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    await terms_consents.accept(callback.from_user.id)
    plan = WEEK_PLAN if parts[1] == "w" else MONTH_PLAN
    payment_url, price = _payment_details(plan, settings)
    if not payment_url:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status == "pending":
        await callback.answer("Загрузите чек оплаты.", show_alert=True)
        return
    await service.begin_payment(user_id=callback.from_user.id, apartment_id=apartment_id,
                                username=callback.from_user.username,
                                first_name=callback.from_user.first_name, plan=plan)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=payment_keyboard(
            apartment_id, signer=signer, payment_url=payment_url,
            support_url=settings.support_bot_url, price=price, agreed=True))


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
            "Выберите тариф доступа к контактам объявлений.",
            reply_markup=private_payment_keyboard(
                apartment_id,
                signer=signer,
                payment_url=settings.finik_payment_url,
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
        await payments.mark_payment_claimed(
            user_id=callback.from_user.id,
            apartment_id=apartment_id,
        )
        payment_url, price = _payment_details(result.plan, settings)
        await callback.answer("Загрузите чек оплаты.", show_alert=True)
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
        payment_url, price = _payment_details(result.plan, settings)
        await callback.answer(
            "Загрузите чек оплаты.",
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
        payment_url, price = _payment_details(result.plan, settings)
        await callback.answer("После оплаты загрузите чек.", show_alert=True)
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
            "Откройте оплату повторно или выберите другой тариф.",
            show_alert=True,
        )
        if callback.message:
            reply_markup = (
                private_payment_keyboard(
                    apartment_id,
                    signer=signer,
                    payment_url=settings.finik_payment_url,
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
        "Выберите тариф и после оплаты нажмите «Получить номер».",
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
    state: FSMContext,
) -> None:
    token = (callback.data or "")[len(PAID_PREFIX) :]
    apartment_id = signer.verify_id("paid", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    result = await service.contact_status(callback.from_user.id, apartment_id)
    if result.status in {"awaiting_receipt", "pending"}:
        if callback.message and callback.message.chat.type != "private":
            await callback.answer("Откройте «Получить номер» под квартирой.", show_alert=True)
            return
        await state.set_state(ReceiptUpload.waiting)
        await state.update_data(receipt_apartment_id=apartment_id)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Отправьте чек оплаты: фото или PDF.")
        return
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
    if result.status == "pending":
        await callback.answer("Загрузите чек оплаты.", show_alert=True)
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
                    payment_url=settings.finik_payment_url,
                    support_url=settings.support_bot_url,
                    monthly_payment_url=settings.monthly_finik_payment_url,
                )
            )
        except Exception:
            logger.exception("Could not replace legacy payment keyboard")


@router.message(ReceiptUpload.waiting)
async def receipt_upload_handler(
    message: Message, state: FSMContext, payments: PaymentRepository,
    service: PaymentService, settings: Settings, bot: Bot,
) -> None:
    file_id = None
    file_type = "document"
    file_size = 0
    if message.photo:
        photo = message.photo[-1]
        file_id, file_size, file_type = photo.file_id, photo.file_size or 0, "photo"
    elif message.document and message.document.mime_type in {
        "application/pdf", "image/jpeg", "image/png", "image/webp",
    }:
        file_id, file_size = message.document.file_id, message.document.file_size or 0
    if not file_id:
        await message.answer("Отправьте фото чека или PDF.")
        return
    if file_size > 10 * 1024 * 1024:
        await message.answer("Размер файла — до 10 МБ.")
        return
    data = await state.get_data()
    apartment_id = data.get("receipt_apartment_id")
    if apartment_id is None:
        await state.clear()
        await message.answer("Откройте оплату повторно.")
        return
    request = await payments.grant_with_receipt(
        user_id=message.from_user.id, apartment_id=apartment_id,
        file_id=file_id, file_type=file_type,
    )
    if request is None or request.status != "approved":
        await message.answer("Откройте оплату повторно.")
        return
    await state.clear()
    result = await service.contact_status(message.from_user.id, apartment_id)
    if result.status == "approved" and result.apartment:
        await send_private_contact(
            bot, user_id=message.from_user.id, apartment=result.apartment,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )

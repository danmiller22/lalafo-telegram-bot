from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.security import TokenSigner
from app.wanted.formatting import format_wanted_admin, format_wanted_ad, format_wanted_preview
from app.wanted.keyboards import (
    contact_keyboard,
    form_cancel_keyboard,
    main_menu_keyboard,
    notes_keyboard,
    preview_keyboard,
    rooms_keyboard,
    wanted_admin_keyboard,
    wanted_payment_keyboard,
)
from app.wanted.repository import WantedAdRepository
from app.wanted.states import WantedAdForm

logger = logging.getLogger(__name__)
router = Router(name="wanted-user")


async def begin_wanted_form(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WantedAdForm.rooms)
    await message.answer(
        "🔎 Создание заявки «Ищу квартиру»\n\nСколько комнат вам нужно?",
        reply_markup=rooms_keyboard(),
    )


@router.message(F.chat.type == "private", Command("want"))
async def wanted_command(message: Message, state: FSMContext) -> None:
    await begin_wanted_form(message, state)


@router.callback_query(F.data == "wanted:new")
async def wanted_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат бота.", show_alert=True)
        return
    await callback.answer()
    await begin_wanted_form(callback.message, state)


@router.message(Command("cancel"))
async def wanted_cancel_command(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    await state.clear()
    await message.answer("Создание заявки отменено.", reply_markup=main_menu_keyboard(settings.support_bot_url))


@router.callback_query(F.data == "wanted:cancel")
async def wanted_cancel(
    callback: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text(
            "Создание заявки отменено.",
            reply_markup=main_menu_keyboard(settings.support_bot_url),
        )


@router.callback_query(StateFilter(WantedAdForm.rooms), F.data.startswith("wanted:room:"))
async def wanted_rooms(callback: CallbackQuery, state: FSMContext) -> None:
    room = (callback.data or "").rsplit(":", 1)[-1]
    if room not in {"studio", "1", "2", "3", "4+"}:
        await callback.answer("Выберите вариант на кнопке.", show_alert=True)
        return
    await state.update_data(rooms=room)
    await state.set_state(WantedAdForm.district)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "📍 В каком районе ищете?\n\nНапример: Центр, 7 мкр, Асанбай.",
            reply_markup=form_cancel_keyboard(),
        )


@router.message(StateFilter(WantedAdForm.district), F.text)
async def wanted_district(message: Message, state: FSMContext) -> None:
    district = (message.text or "").strip()
    if not 2 <= len(district) <= 100:
        await message.answer("Введите район текстом — от 2 до 100 символов.")
        return
    await state.update_data(district=district)
    await state.set_state(WantedAdForm.budget)
    await message.answer(
        "💰 Какой максимальный бюджет за месяц?\n\nНапример: 30000",
        reply_markup=form_cancel_keyboard(),
    )


@router.message(StateFilter(WantedAdForm.budget), F.text)
async def wanted_budget(message: Message, state: FSMContext) -> None:
    digits = re.sub(r"\D", "", message.text or "")
    budget = int(digits) if digits else 0
    if not 5_000 <= budget <= 500_000:
        await message.answer("Введите бюджет числом от 5 000 до 500 000 сом.")
        return
    await state.update_data(budget=budget)
    await state.set_state(WantedAdForm.move_in)
    await message.answer(
        "📅 Когда планируете заселиться?\n\nНапример: с 1 сентября или сразу.",
        reply_markup=form_cancel_keyboard(),
    )


@router.message(StateFilter(WantedAdForm.move_in), F.text)
async def wanted_move_in(message: Message, state: FSMContext) -> None:
    move_in = (message.text or "").strip()
    if not 2 <= len(move_in) <= 100:
        await message.answer("Напишите срок заселения — от 2 до 100 символов.")
        return
    await state.update_data(move_in=move_in)
    await state.set_state(WantedAdForm.tenants)
    await message.answer(
        "👥 Кто будет жить?\n\nНапример: семейная пара без детей или один студент.",
        reply_markup=form_cancel_keyboard(),
    )


@router.message(StateFilter(WantedAdForm.tenants), F.text)
async def wanted_tenants(message: Message, state: FSMContext) -> None:
    tenants = (message.text or "").strip()
    if not 2 <= len(tenants) <= 200:
        await message.answer("Опишите жильцов — от 2 до 200 символов.")
        return
    await state.update_data(tenants=tenants)
    await state.set_state(WantedAdForm.notes)
    await message.answer(
        "📝 Дополнительно?\n\nНапример: с мебелью, можно с котом.",
        reply_markup=notes_keyboard(),
    )


async def _ask_contact(message: Message, state: FSMContext, username: str | None) -> None:
    await state.set_state(WantedAdForm.contact)
    await message.answer(
        "📞 Укажите контакт для объявления: номер телефона или @username.\n\n"
        "Этот контакт будет виден в опубликованной заявке.",
        reply_markup=contact_keyboard(username),
    )


@router.message(StateFilter(WantedAdForm.notes), F.text)
async def wanted_notes(message: Message, state: FSMContext) -> None:
    notes = (message.text or "").strip()
    if not 2 <= len(notes) <= 500:
        await message.answer(
            "Введите дополнительную информацию до 500 символов или нажмите «Пропустить»."
        )
        return
    await state.update_data(notes=notes)
    await _ask_contact(message, state, message.from_user.username)


@router.callback_query(StateFilter(WantedAdForm.notes), F.data == "wanted:notes:skip")
async def wanted_notes_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(notes="Без дополнительных пожеланий")
    await callback.answer()
    if callback.message:
        await _ask_contact(callback.message, state, callback.from_user.username)


async def _show_preview(message: Message, state: FSMContext, contact: str) -> None:
    await state.update_data(contact=contact)
    await state.set_state(WantedAdForm.confirm)
    data = await state.get_data()
    await message.answer(
        "Проверьте заявку перед оплатой:\n\n" + format_wanted_preview(data),
        reply_markup=preview_keyboard(),
    )


@router.message(StateFilter(WantedAdForm.contact), F.text)
async def wanted_contact(message: Message, state: FSMContext) -> None:
    contact = (message.text or "").strip()
    if not 3 <= len(contact) <= 100:
        await message.answer("Введите телефон или @username — до 100 символов.")
        return
    await _show_preview(message, state, contact)


@router.callback_query(StateFilter(WantedAdForm.contact), F.data == "wanted:contact:self")
async def wanted_contact_self(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user.username:
        await callback.answer("У вашего аккаунта нет @username.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _show_preview(callback.message, state, f"@{callback.from_user.username}")


@router.callback_query(StateFilter(WantedAdForm.confirm), F.data == "wanted:create")
async def wanted_create(
    callback: CallbackQuery,
    state: FSMContext,
    wanted_ads: WantedAdRepository,
    signer: TokenSigner,
    settings: Settings,
) -> None:
    data = await state.get_data()
    required = {"rooms", "district", "budget", "move_in", "tenants", "notes", "contact"}
    if not required.issubset(data):
        await state.clear()
        await callback.answer("Анкета устарела. Заполните её заново.", show_alert=True)
        return
    ad = await wanted_ads.create(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        rooms=str(data["rooms"]),
        district=str(data["district"]),
        budget=int(data["budget"]),
        move_in=str(data["move_in"]),
        tenants=str(data["tenants"]),
        notes=str(data["notes"]),
        contact=str(data["contact"]),
    )
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Заявка сохранена. Стоимость публикации — 100 сом.\n\n"
            + format_wanted_ad(ad)
            + "\n\nПосле оплаты нажмите «Проверить оплату».",
            reply_markup=wanted_payment_keyboard(
                ad.id,
                signer=signer,
                payment_url=settings.wanted_finik_payment_url,
                support_url=settings.support_bot_url,
            ),
        )


@router.callback_query(F.data.startswith("wanted-paid:"))
async def wanted_paid(
    callback: CallbackQuery,
    wanted_ads: WantedAdRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    token = (callback.data or "").split(":", 1)[-1]
    ad_id = signer.verify_id("wanted-paid", token)
    if ad_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    outcome, ad = await wanted_ads.submit_payment(ad_id, callback.from_user.id)
    if outcome == "missing" or ad is None:
        await callback.answer("Заявка не найдена или принадлежит другому пользователю.", show_alert=True)
        return
    if outcome == "published":
        await callback.answer("✅ Заявка уже опубликована в группе.", show_alert=True)
        return
    if outcome == "publishing":
        await callback.answer("⏳ Заявка публикуется. Подождите немного.", show_alert=True)
        return
    await callback.answer(
        "⏳ Оплата отправлена на проверку. После подтверждения заявка автоматически "
        "появится в группе. Повторно оплачивать не нужно.",
        show_alert=True,
    )
    if callback.message:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=wanted_payment_keyboard(
                    ad.id,
                    signer=signer,
                    payment_url=settings.wanted_finik_payment_url,
                    support_url=settings.support_bot_url,
                    pending=True,
                )
            )
        except Exception:
            logger.exception("Could not update wanted ad payment keyboard")
    if not settings.admin_user_id or not await wanted_ads.claim_admin_notification(ad.id):
        return
    try:
        admin_message = await bot.send_message(
            settings.admin_user_id,
            format_wanted_admin(ad),
            reply_markup=wanted_admin_keyboard(ad.id, signer=signer),
        )
    except Exception as exc:
        await wanted_ads.release_admin_notification(ad.id)
        logger.error("Wanted ad admin notification failed: %s", type(exc).__name__)
        return
    await wanted_ads.finish_admin_notification(ad.id, admin_message.message_id)


@router.message(F.chat.type == "private", Command("mywanted"))
async def my_wanted_ads(
    message: Message,
    wanted_ads: WantedAdRepository,
    settings: Settings,
    signer: TokenSigner,
) -> None:
    await _send_my_wanted_ads(
        message,
        user_id=message.from_user.id,
        wanted_ads=wanted_ads,
        settings=settings,
        signer=signer,
    )


@router.callback_query(F.data == "menu:mywanted")
async def my_wanted_ads_button(
    callback: CallbackQuery,
    wanted_ads: WantedAdRepository,
    settings: Settings,
    signer: TokenSigner,
) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат бота.", show_alert=True)
        return
    await callback.answer()
    await _send_my_wanted_ads(
        callback.message,
        user_id=callback.from_user.id,
        wanted_ads=wanted_ads,
        settings=settings,
        signer=signer,
    )


async def _send_my_wanted_ads(
    message: Message,
    *,
    user_id: int,
    wanted_ads: WantedAdRepository,
    settings: Settings,
    signer: TokenSigner,
) -> None:
    rows = await wanted_ads.owned(user_id)
    status_labels = {
        "awaiting_payment": "ожидает оплаты",
        "pending": "на проверке",
        "publishing": "публикуется",
        "published": "опубликована",
        "rejected": "оплата отклонена",
    }
    if not rows:
        await message.answer(
            "У вас пока нет заявок.", reply_markup=main_menu_keyboard(settings.support_bot_url)
        )
        return
    await message.answer("Ваши последние заявки:")
    for ad in rows[:5]:
        keyboard = None
        if ad.status in {"awaiting_payment", "rejected", "pending"}:
            keyboard = wanted_payment_keyboard(
                ad.id,
                signer=signer,
                payment_url=settings.wanted_finik_payment_url,
                support_url=settings.support_bot_url,
                pending=ad.status == "pending",
            )
        await message.answer(
            f"Заявка #{ad.id} · {status_labels.get(ad.status, ad.status)}\n\n"
            + format_wanted_ad(ad),
            reply_markup=keyboard,
        )
    await message.answer(
        "Чтобы разместить новую заявку, нажмите кнопку ниже.",
        reply_markup=main_menu_keyboard(settings.support_bot_url),
    )

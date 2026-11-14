from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.matching.keyboards import (
    budget_keyboard,
    district_preset_members,
    districts_keyboard,
    feed_footer_keyboard,
    hot_start_keyboard,
    rooms_keyboard,
    search_home_keyboard,
    seller_types_keyboard,
)
from app.matching.presentation import parse_budget, profile_summary
from app.matching.repository import MAX_PRICE, MIN_PRICE, MatchingRepository
from app.matching.states import ApartmentMatchForm
from app.models import ApartmentSearchProfile
from app.payment_plans import PERSONAL_LIMIT
from app.security import TokenSigner
from app.telegram.private_delivery import send_matching_card

router = Router(name="apartment-matching")


@router.callback_query(F.data == "personal:offer")
async def personal_offer(callback: CallbackQuery, matching: MatchingRepository) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.answer(
        "Для новых клиентов доступен недельный тариф — 499 сом на 7 дней. "
        "Откройте карточку квартиры и нажмите «Получить номер»."
    )


@router.callback_query(F.data == "personal:buy")
async def personal_buy(callback: CallbackQuery) -> None:
    await callback.answer(
        "Доступен только недельный тариф — 499 сом. Откройте карточку квартиры.", show_alert=True
    )


async def _send_cards(
    bot: Bot,
    *,
    user_id: int,
    apartments: list,
    signer: TokenSigner,
    settings: Settings,
) -> None:
    for apartment in apartments:
        await send_matching_card(
            bot,
            user_id=user_id,
            apartment=apartment,
            signer=signer,
            bot_username=settings.telegram_bot_username,
        )


async def show_hot_start(
    message: Message,
    *,
    bot: Bot,
    matching: MatchingRepository,
    signer: TokenSigner,
    settings: Settings,
    state: FSMContext,
    source: str,
) -> None:
    user_id = message.from_user.id
    profile = await matching.profile(user_id)
    await state.clear()
    await state.update_data(matching_source=source)
    await matching.event(user_id, "start", source=source)
    if profile is None:
        text = (
            "🏠 Найдите квартиру под себя в Бишкеке\n\n"
            "Выберите количество комнат, районы и бюджет. Бот покажет подходящие "
            "квартиры и будет присылать новые совпадения.\n\n"
            "Просмотр и подбор бесплатны. Контакты открываются по доступу: "
            "499 сом на 7 дней. Ранее оплаченный бессрочный доступ сохраняется."
        )
    else:
        text = profile_summary(profile)
    await message.answer(text, reply_markup=search_home_keyboard(profile))


async def begin_matching_form(
    message: Message,
    state: FSMContext,
    *,
    source: str = "telegram",
    profile: ApartmentSearchProfile | None = None,
) -> None:
    previous = await state.get_data()
    hot_ids = previous.get("hot_apartment_ids", [])
    await state.clear()
    await state.set_state(ApartmentMatchForm.rooms)
    await state.update_data(
        matching_source=previous.get("matching_source", source),
        hot_apartment_ids=hot_ids,
        matching_rooms=list(profile.rooms or []) if profile is not None else [],
        matching_seller_types=list(profile.seller_types or [])
        if profile is not None
        else ["owner", "realtor"],
        matching_districts=list(profile.districts or []) if profile is not None else [],
        all_districts=bool(profile.all_districts) if profile is not None else False,
    )
    await message.answer(
        "1/4 · Комнаты\nМожно выбрать несколько вариантов.",
        reply_markup=rooms_keyboard(set(profile.rooms or []) if profile is not None else set()),
    )


@router.callback_query(F.data == "matching:start")
async def matching_start(
    callback: CallbackQuery, state: FSMContext, matching: MatchingRepository
) -> None:
    await callback.answer()
    if callback.message:
        await begin_matching_form(
            callback.message, state, profile=await matching.profile(callback.from_user.id)
        )


@router.callback_query(StateFilter(ApartmentMatchForm.rooms), F.data.startswith("matching:room:"))
async def matching_room(callback: CallbackQuery, state: FSMContext) -> None:
    value = (callback.data or "").rsplit(":", 1)[-1]
    if value not in {"studio", "1", "2"}:
        await callback.answer("Выберите вариант на кнопке.", show_alert=True)
        return
    data = await state.get_data()
    selected = set(data.get("matching_rooms", []))
    selected.symmetric_difference_update({value})
    await state.update_data(matching_rooms=sorted(selected))
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=rooms_keyboard(selected))


@router.callback_query(StateFilter(ApartmentMatchForm.rooms), F.data == "matching:rooms:done")
async def matching_rooms_done(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    if not data.get("matching_rooms"):
        await callback.answer("Выберите хотя бы один тип квартиры.", show_alert=True)
        return
    await state.set_state(ApartmentMatchForm.seller_types)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "2/4 · Объявления\nМожно оставить оба варианта или выбрать один.",
            reply_markup=seller_types_keyboard(set(data.get("matching_seller_types", []))),
        )


@router.callback_query(
    StateFilter(ApartmentMatchForm.seller_types),
    F.data.startswith("matching:seller:"),
)
async def matching_seller_type(callback: CallbackQuery, state: FSMContext) -> None:
    value = (callback.data or "").rsplit(":", 1)[-1]
    if value not in {"owner", "realtor"}:
        await callback.answer("Выберите вариант на кнопке.", show_alert=True)
        return
    data = await state.get_data()
    selected = set(data.get("matching_seller_types", []))
    selected.symmetric_difference_update({value})
    await state.update_data(matching_seller_types=sorted(selected))
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=seller_types_keyboard(selected))


@router.callback_query(
    StateFilter(ApartmentMatchForm.seller_types), F.data == "matching:sellers:done"
)
async def matching_seller_types_done(
    callback: CallbackQuery,
    state: FSMContext,
    matching: MatchingRepository,
) -> None:
    data = await state.get_data()
    if not data.get("matching_seller_types"):
        await callback.answer("Выберите хотя бы один вариант.", show_alert=True)
        return
    options = await matching.available_districts()
    if not options:
        options = ["Центр"]
    await state.set_state(ApartmentMatchForm.districts)
    await state.update_data(
        district_options=options,
        district_page=0,
    )
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "3/4 · Районы\nВыберите конкретные районы или весь Бишкек.",
            reply_markup=districts_keyboard(
                options,
                set(data.get("matching_districts", [])),
                page=0,
                all_districts=bool(data.get("all_districts", False)),
            ),
        )


async def _render_districts(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=districts_keyboard(
                list(data.get("district_options", [])),
                set(data.get("matching_districts", [])),
                page=int(data.get("district_page", 0)),
                all_districts=bool(data.get("all_districts", False)),
            )
        )


@router.callback_query(
    StateFilter(ApartmentMatchForm.districts), F.data.startswith("matching:district:")
)
async def matching_district(callback: CallbackQuery, state: FSMContext) -> None:
    token = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    options = list(data.get("district_options", []))
    selected = set(data.get("matching_districts", []))
    if token == "all":
        all_districts = not bool(data.get("all_districts", False))
        selected.clear()
    else:
        try:
            value = options[int(token)]
        except (ValueError, IndexError):
            await callback.answer("Список районов обновился.", show_alert=True)
            return
        all_districts = False
        selected.symmetric_difference_update({value})
    await state.update_data(matching_districts=sorted(selected), all_districts=all_districts)
    await callback.answer()
    await _render_districts(callback, state)


@router.callback_query(
    StateFilter(ApartmentMatchForm.districts), F.data.startswith("matching:preset:")
)
async def matching_district_preset(callback: CallbackQuery, state: FSMContext) -> None:
    key = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    options = list(data.get("district_options", []))
    selected = set(data.get("matching_districts", []))
    members = district_preset_members(options, key)
    if not members:
        await callback.answer("В этой группе пока нет районов.", show_alert=True)
        return
    if members.issubset(selected):
        selected.difference_update(members)
    else:
        selected.update(members)
    await state.update_data(matching_districts=sorted(selected), all_districts=False)
    await callback.answer()
    await _render_districts(callback, state)


@router.callback_query(
    StateFilter(ApartmentMatchForm.districts), F.data.startswith("matching:page:")
)
async def matching_district_page(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    await state.update_data(district_page=max(0, page))
    await callback.answer()
    await _render_districts(callback, state)


@router.callback_query(F.data == "matching:noop")
async def matching_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(
    StateFilter(ApartmentMatchForm.districts), F.data == "matching:districts:done"
)
async def matching_districts_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("all_districts") and not data.get("matching_districts"):
        await callback.answer("Выберите районы или весь Бишкек.", show_alert=True)
        return
    await state.set_state(ApartmentMatchForm.budget)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "4/4 · Бюджет\nДо какой суммы в месяц ищем? Можно написать свою сумму.",
            reply_markup=budget_keyboard(),
        )


@router.callback_query(StateFilter(ApartmentMatchForm.budget), F.data == "matching:budget:custom")
async def matching_custom_budget(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Напишите бюджет от 5 000 до 60 000 сом. Например: 30 000 или 30 тыс.",
            reply_markup=budget_keyboard(),
        )


async def _finish_profile(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    budget: int,
    matching: MatchingRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    data = await state.get_data()
    profile = await matching.save_profile(
        user_id=user_id,
        rooms=list(data.get("matching_rooms", [])),
        districts=list(data.get("matching_districts", [])),
        all_districts=bool(data.get("all_districts", False)),
        max_budget=budget,
        source=str(data.get("matching_source", "telegram")),
        seller_types=list(data.get("matching_seller_types", [])),
    )
    grant, used = await matching.personal_status(user_id)
    if grant is not None:
        await state.clear()
        await message.answer(
            f"✅ Требования сохранены. Вариантов в подборе: {used}/{PERSONAL_LIMIT}. "
            "Новые совпадения будут приходить автоматически в течение срока тарифа.",
            reply_markup=feed_footer_keyboard(has_more=False),
        )
        return
    excluded = {int(value) for value in data.get("hot_apartment_ids", [])}
    apartments = await matching.matching_apartments(
        profile, limit=3, exclude_ids=excluded, exclude_delivered=True
    )
    await matching.event(user_id, "filter_saved", source=profile.source)
    await state.clear()
    if apartments:
        await message.answer(
            "✅ Подбор сохранён.\n\n" + profile_summary(profile) + "\n\nВот подходящие квартиры:"
        )
        await _send_cards(
            bot,
            user_id=user_id,
            apartments=apartments,
            signer=signer,
            settings=settings,
        )
        await matching.record_shown(user_id, apartments, kind="initial")
    else:
        await message.answer(
            "✅ Подбор сохранён.\n\n"
            + profile_summary(profile)
            + "\n\nСейчас совпадений нет. Можно расширить районы или бюджет — либо дождаться новых вариантов."
        )
    await message.answer(
        "Новые совпадения будут приходить автоматически.",
        reply_markup=feed_footer_keyboard(
            has_more=len(apartments) == 3, notifications_enabled=profile.notifications_enabled
        ),
    )


@router.callback_query(
    StateFilter(ApartmentMatchForm.budget), F.data.startswith("matching:budget:")
)
async def matching_budget_button(
    callback: CallbackQuery,
    state: FSMContext,
    matching: MatchingRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    token = (callback.data or "").rsplit(":", 1)[-1]
    if token == "custom":
        return
    try:
        budget = int(token)
    except ValueError:
        await callback.answer("Выберите бюджет на кнопке.", show_alert=True)
        return
    if not MIN_PRICE <= budget <= MAX_PRICE:
        await callback.answer("Бюджет должен быть от 5 000 до 60 000 сом.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _finish_profile(
            callback.message,
            state,
            user_id=callback.from_user.id,
            budget=budget,
            matching=matching,
            signer=signer,
            settings=settings,
            bot=bot,
        )


@router.message(StateFilter(ApartmentMatchForm.budget), F.text)
async def matching_budget_text(
    message: Message,
    state: FSMContext,
    matching: MatchingRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    budget = parse_budget(message.text or "")
    if budget is None or not MIN_PRICE <= budget <= MAX_PRICE:
        await message.answer("Введите сумму от 5 000 до 60 000 сом.")
        return
    await _finish_profile(
        message,
        state,
        user_id=message.from_user.id,
        budget=budget,
        matching=matching,
        signer=signer,
        settings=settings,
        bot=bot,
    )


@router.callback_query(F.data.in_({"matching:mine", "matching:more"}))
async def matching_feed(
    callback: CallbackQuery,
    matching: MatchingRepository,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    profile = await matching.profile(callback.from_user.id)
    if profile is None:
        await callback.answer("Сначала настройте подбор.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Выберите параметры квартиры.", reply_markup=hot_start_keyboard()
            )
        return
    grant, used = await matching.personal_status(callback.from_user.id)
    if grant is not None:
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"✨ Под меня: {used}/{PERSONAL_LIMIT} вариантов. "
                "Новые совпадения приходят автоматически до окончания тарифа.",
                reply_markup=feed_footer_keyboard(has_more=False),
            )
        return
    apartments = await matching.matching_apartments(
        profile, limit=3, exclude_delivered=callback.data == "matching:more"
    )
    await callback.answer()
    if callback.message:
        if apartments:
            await _send_cards(
                bot,
                user_id=callback.from_user.id,
                apartments=apartments,
                signer=signer,
                settings=settings,
            )
            await matching.record_shown(callback.from_user.id, apartments, kind="manual")
        else:
            await callback.message.answer("Новых совпадений пока нет.")
        await callback.message.answer(
            "Подборка обновлена.",
            reply_markup=feed_footer_keyboard(
                has_more=len(apartments) == 3, notifications_enabled=profile.notifications_enabled
            ),
        )


@router.callback_query(F.data == "matching:toggle")
async def matching_toggle(callback: CallbackQuery, matching: MatchingRepository) -> None:
    enabled = await matching.toggle_notifications(callback.from_user.id)
    if enabled is None:
        await callback.answer("Сначала настройте подбор.", show_alert=True)
    else:
        await callback.answer(
            "🔔 Уведомления включены." if enabled else "🔕 Уведомления приостановлены."
        )
        profile = await matching.profile(callback.from_user.id)
        if callback.message and profile is not None:
            await callback.message.edit_text(
                profile_summary(profile), reply_markup=search_home_keyboard(profile)
            )


@router.callback_query(F.data == "matching:home")
async def matching_home(
    callback: CallbackQuery, state: FSMContext, matching: MatchingRepository
) -> None:
    await state.clear()
    await callback.answer()
    profile = await matching.profile(callback.from_user.id)
    if callback.message:
        text = (
            profile_summary(profile)
            if profile is not None
            else "🏠 Настройте поиск: комнаты, районы и бюджет."
        )
        await callback.message.answer(text, reply_markup=search_home_keyboard(profile))


@router.callback_query(StateFilter(ApartmentMatchForm), F.data.startswith("matching:back:"))
async def matching_back(callback: CallbackQuery, state: FSMContext) -> None:
    target = (callback.data or "").rsplit(":", 1)[-1]
    data = await state.get_data()
    if target == "rooms":
        next_state = ApartmentMatchForm.rooms
        text = "1/4 · Комнаты\nМожно выбрать несколько вариантов."
        keyboard = rooms_keyboard(set(data.get("matching_rooms", [])))
    elif target == "sellers":
        next_state = ApartmentMatchForm.seller_types
        text = "2/4 · Объявления\nМожно оставить оба варианта или выбрать один."
        keyboard = seller_types_keyboard(set(data.get("matching_seller_types", [])))
    elif target == "districts":
        next_state = ApartmentMatchForm.districts
        text = "3/4 · Районы\nВыберите конкретные районы или весь Бишкек."
        keyboard = districts_keyboard(
            list(data.get("district_options", [])),
            set(data.get("matching_districts", [])),
            page=int(data.get("district_page", 0)),
            all_districts=bool(data.get("all_districts", False)),
        )
    else:
        await callback.answer()
        return
    await state.set_state(next_state)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("matching:"))
async def matching_expired(callback: CallbackQuery) -> None:
    await callback.answer("Откройте поиск заново — сохранённые параметры на месте.")
    if callback.message:
        await callback.message.answer(
            "Вернитесь к своему поиску.", reply_markup=search_home_keyboard()
        )

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.config import Settings
from app.favorites import FavoriteRepository
from app.matching.handlers import begin_matching_form
from app.payments.repository import ApartmentRepository
from app.payments.service import PaymentService
from app.security import TokenSigner
from app.showcase.service import ShowcaseService
from app.telegram.formatting import format_showcase_apartment
from app.telegram.formatting import is_confirmed_owner
from app.telegram.keyboards import (
    favorite_apartment_keyboard,
    private_payment_keyboard,
    showcase_apartment_keyboard,
    terms_keyboard,
)
from app.telegram.private_delivery import send_private_contact
from app.terms import PRIVACY_TEXT, TERMS_TEXT, TermsConsentRepository


router = Router(name="showcase")


@router.callback_query(F.data.startswith("listing:save:"))
async def save_listing(
    callback: CallbackQuery,
    signer: TokenSigner,
    favorites: FavoriteRepository,
    apartments: ApartmentRepository,
    settings: Settings,
) -> None:
    token = (callback.data or "").removeprefix("listing:save:")
    apartment_id = signer.verify_id("listing-save", token)
    apartment = await apartments.get(apartment_id) if apartment_id is not None else None
    if apartment is None:
        await callback.answer("Квартира недоступна.", show_alert=True)
        return
    await favorites.save(callback.from_user.id, apartment.id)
    await callback.answer("Сохранено")
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=showcase_apartment_keyboard(
                apartment.id, signer=signer, saved=True,
                bot_username=settings.telegram_bot_username,
                phone=apartment.phone, confirmed_owner=is_confirmed_owner(apartment),
            )
        )


@router.callback_query(F.data == "favorite:noop")
async def saved_noop(callback: CallbackQuery) -> None:
    await callback.answer("Квартира уже сохранена.")


@router.callback_query(F.data.startswith("favorite:remove:"))
async def remove_favorite(
    callback: CallbackQuery,
    signer: TokenSigner,
    favorites: FavoriteRepository,
) -> None:
    token = (callback.data or "").removeprefix("favorite:remove:")
    apartment_id = signer.verify_id("favorite-remove", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    await favorites.remove(callback.from_user.id, apartment_id)
    await callback.answer("Удалено из избранного")
    if callback.message:
        await callback.message.delete()


@router.callback_query(F.data == "menu:favorites")
async def show_favorites(
    callback: CallbackQuery,
    favorites: FavoriteRepository,
    signer: TokenSigner,
    bot: Bot,
) -> None:
    rows = await favorites.apartments(callback.from_user.id)
    await callback.answer()
    if not rows:
        if callback.message:
            await callback.message.answer("В избранном пока нет квартир.")
        return
    for apartment in rows:
        available = bool(
            apartment.active and apartment.availability_status != "unavailable"
        )
        text = format_showcase_apartment(apartment)
        if not available:
            text += "\n\nНедоступно"
        keyboard = favorite_apartment_keyboard(
            apartment.id, signer=signer, available=available
        )
        photo = next(iter(dict.fromkeys(apartment.photo_urls)), None)
        if photo:
            from aiogram.types import URLInputFile

            try:
                await bot.send_photo(
                    callback.from_user.id,
                    URLInputFile(photo, timeout=25),
                    caption=text,
                    reply_markup=keyboard,
                )
                continue
            except Exception:
                pass
        await bot.send_message(callback.from_user.id, text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:cheap")
async def cheap_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await begin_matching_form(callback.message, state, source="telegram")


@router.callback_query(F.data == "menu:about")
async def about_service(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Arenda.\u2060KG помогает находить квартиры и получать контакты по объявлениям.\n\n"
            "Просмотр квартир и сохранение в избранное бесплатны. Доступ ко всем "
            "контактам: 7 дней — 499 сом или навсегда — 999 сом. Автоматического "
            "продления нет."
        )


@router.callback_query(F.data == "menu:privacy")
async def showcase_privacy(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(PRIVACY_TEXT)


async def _open_contact_or_plans(
    callback: CallbackQuery,
    *,
    apartment_id: int,
    apartments: ApartmentRepository,
    service: PaymentService,
    signer: TokenSigner,
    settings: Settings,
    bot: Bot,
) -> None:
    apartment = await apartments.get(apartment_id)
    if apartment is None or not apartment.phone:
        if callback.message:
            await callback.message.answer("Квартира недоступна.")
        return
    access = await service.payments.active_weekly_access(callback.from_user.id)
    if access is not None:
        await send_private_contact(
            bot,
            user_id=callback.from_user.id,
            apartment=apartment,
            support_url=settings.support_bot_url,
            max_photos=settings.max_photos_per_apartment,
        )
        return
    if callback.message:
        await callback.message.answer(
            "Выберите срок доступа ко всем контактам:",
            reply_markup=private_payment_keyboard(
                apartment.id,
                signer=signer,
                payment_url=settings.weekly_finik_payment_url,
                monthly_payment_url=settings.monthly_finik_payment_url,
                support_url=settings.support_bot_url,
            ),
        )


@router.callback_query(F.data.startswith("listing:contact:"))
async def listing_contact(
    callback: CallbackQuery,
    signer: TokenSigner,
    showcase: ShowcaseService,
    terms_consents: TermsConsentRepository,
    apartments: ApartmentRepository,
    service: PaymentService,
    settings: Settings,
    bot: Bot,
) -> None:
    token = (callback.data or "").removeprefix("listing:contact:")
    apartment_id = signer.verify_id("listing-contact", token)
    if apartment_id is None:
        await callback.answer("Недействительная кнопка.", show_alert=True)
        return
    await callback.answer()
    try:
        current_ids = await showcase.current_apartment_ids()
    except Exception:
        if callback.message:
            await callback.message.answer(
                "Не удалось проверить квартиру. Попробуйте позже."
            )
        return
    if apartment_id not in current_ids:
        if callback.message:
            await callback.message.answer("Эта квартира уже снята с публикации.")
        return
    if not await terms_consents.accepted(callback.from_user.id):
        if callback.message:
            await callback.message.answer(
                TERMS_TEXT,
                reply_markup=terms_keyboard(
                    apartment_id,
                    signer=signer,
                    bot_username=settings.telegram_bot_username,
                ),
            )
        return
    await _open_contact_or_plans(
        callback,
        apartment_id=apartment_id,
        apartments=apartments,
        service=service,
        signer=signer,
        settings=settings,
        bot=bot,
    )

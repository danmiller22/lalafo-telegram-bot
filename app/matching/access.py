"""Paid access boundaries for private apartment matching."""
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from app.telegram.keyboards import MINI_APP_SHORT_NAME

async def show_matching_paywall(message, *, matching, signer, settings):
    rows = []
    apartments = await matching._fresh_apartments()
    apartment = next((a for a in apartments if a.phone), None)
    if apartment is not None:
        token = signer.sign_start_id('miniapp-apartment', apartment.id)
        url = f"https://t.me/{settings.telegram_bot_username.lstrip('@')}/{MINI_APP_SHORT_NAME}?startapp=personal_{token}"
        rows.append([InlineKeyboardButton(text='Под вас — 999 сом', url=url)])
    rows.extend([
        [InlineKeyboardButton(text='Смотреть квартиры в группе', url='https://t.me/rentifykg')],
        [InlineKeyboardButton(text='❤️ Избранное', callback_data='menu:favorites')],
        [InlineKeyboardButton(text='🛟 Помощь', callback_data='menu:support')],
    ])
    await message.answer(
        'Подбор по районам и бюджету доступен только по тарифу «Под вас» — 999 сом на 30 дней. '
        'После оплаты настройте фильтры и получайте подходящие квартиры с номерами в личный чат. '
        'Доступ к контактам за 499 сом не включает персональный подбор.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def open_paid_matching(message, state, *, matching, signer, settings, user_id=None, **kwargs):
    from app.matching.handlers import begin_matching_form
    user_id = user_id if user_id is not None else message.from_user.id
    grant, _ = await matching.personal_status(user_id)
    if grant is None:
        await state.clear()
        await show_matching_paywall(message, matching=matching, signer=signer, settings=settings)
        return
    await begin_matching_form(message, state, **kwargs)

class PaidMatchingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        matching = data['matching']
        user_id = event.from_user.id
        grant, _ = await matching.personal_status(user_id)
        if grant is not None:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == 'matching:toggle':
            profile = await matching.profile(user_id)
            if profile is not None and profile.notifications_enabled:
                return await handler(event, data)  # Stopping is always permitted.
        state = data.get('state')
        if state is not None:
            await state.clear()
        message = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer()
        if message is not None:
            await show_matching_paywall(message, matching=matching, signer=data['signer'], settings=data['settings'])

"""Deliver customer payment claims to the configured administrator."""
from __future__ import annotations

import asyncio
from uuid import uuid4
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.models import PaymentRequest
from app.payments.repository import PaymentRepository
from app.security import TokenSigner

from app.payment_plans import plan_label, plan_price


def payment_review_keyboard(request_id: int, signer: TokenSigner) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Дать доступ", callback_data=f"payment:a:{signer.sign_id('payment-approve', request_id)}"),
        InlineKeyboardButton(text="Отказать", callback_data=f"payment:r:{signer.sign_id('payment-reject', request_id)}"),
    ]])


async def notify_payment_admin(
    bot: Bot,
    payments: PaymentRepository,
    settings: Settings,
    signer: TokenSigner,
    request: PaymentRequest,
) -> bool:
    if request.status != "pending":
        return False
    if not settings.admin_user_id:
        raise RuntimeError("Payment administrator is not configured")
    token = uuid4().hex
    if not await payments.claim_admin_notification(request.id, token=token):
        return False
    try:
        username = f"@{request.username}" if request.username else "без username"
        text = (f"Проверка оплаты #{request.id}\n"
                f"Клиент: {request.first_name or ''} {username}\n"
                f"Telegram ID: {request.telegram_user_id}\n"
                f"Тариф: {plan_label(request.plan)} — {plan_price(request.plan)} сом\n"
                "Клиент нажал «Я оплатил(а)».\n\n"
                f"⚠️ Одобрите только после отдельного поступления {plan_price(request.plan)} сом в Finik. "
                "Несколько заявок одного клиента не являются несколькими оплатами.")
        markup = payment_review_keyboard(request.id, signer)
        sent = await bot.send_message(settings.admin_user_id, text, reply_markup=markup)
        return await payments.finish_admin_notification(request.id, sent.message_id, token=token)
    except (Exception, asyncio.CancelledError):
        await payments.release_admin_notification(request.id, token=token)
        raise


async def notify_verified_payment_admin(
    bot: Bot,
    payments: PaymentRepository,
    settings: Settings,
    request: PaymentRequest,
) -> bool:
    """Report one provider-verified automatic grant to the administrator."""
    if request.status != "approved" or request.approved_by is not None:
        return False
    if not settings.admin_user_id:
        raise RuntimeError("Payment administrator is not configured")
    token = uuid4().hex
    if not await payments.claim_provider_admin_notification(request.id, token=token):
        return False
    try:
        username = f"@{request.username}" if request.username else "без username"
        expires = (
            request.access_expires_at.strftime("%d.%m.%Y %H:%M UTC")
            if request.access_expires_at is not None
            else "без ограничения срока"
        )
        text = (
            f"✅ Автоматическая оплата #{request.id}\n"
            f"Клиент: {request.first_name or 'Имя не указано'} — {username}\n"
            f"Telegram ID: {request.telegram_user_id}\n"
            f"Тариф: {plan_label(request.plan)}\n"
            f"Сумма: {plan_price(request.plan)} сом\n"
            f"Квартира ID: {request.apartment_id}\n"
            f"Доступ: {expires}\n\n"
            "Finik подтвердил платёж, доступ выдан автоматически."
        )
        sent = await bot.send_message(settings.admin_user_id, text)
        return await payments.finish_provider_admin_notification(
            request.id, sent.message_id, token=token
        )
    except (Exception, asyncio.CancelledError):
        await payments.release_provider_admin_notification(request.id, token=token)
        raise

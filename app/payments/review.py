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
        payment_evidence = (
            "Finik подтвердил оплату."
            if getattr(request, "provider_status", None) == "succeeded"
            else "Клиент нажал «Я оплатил(а)»."
        )
        text = (f"Проверка оплаты #{request.id}\n"
                f"Клиент: {request.first_name or ''} {username}\n"
                f"Telegram ID: {request.telegram_user_id}\n"
                f"Тариф: {plan_label(request.plan)} — {plan_price(request.plan)} сом\n"
                f"{payment_evidence}\n\n"
                "⚠️ Одобрите только после отдельного поступления 499 сом в Finik. "
                "Несколько заявок одного клиента не являются несколькими оплатами.")
        markup = payment_review_keyboard(request.id, signer)
        sent = await bot.send_message(settings.admin_user_id, text, reply_markup=markup)
        return await payments.finish_admin_notification(request.id, sent.message_id, token=token)
    except (Exception, asyncio.CancelledError):
        await payments.release_admin_notification(request.id, token=token)
        raise

"""Deliver customer payment claims to the configured administrator."""
from __future__ import annotations

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
    if not await payments.claim_admin_notification(request.id):
        return False
    try:
        username = f"@{request.username}" if request.username else "без username"
        text = (f"Проверка оплаты #{request.id}\n"
                f"Клиент: {request.first_name or ''} {username}\n"
                f"Telegram ID: {request.telegram_user_id}\n"
                f"Тариф: {plan_label(request.plan)} — {plan_price(request.plan)} сом\n"
                "Клиент нажал «Я оплатил(а)». Проверьте поступление оплаты.")
        markup = payment_review_keyboard(request.id, signer)
        file_id = getattr(request, "receipt_file_id", None)
        file_type = getattr(request, "receipt_file_type", None)
        if file_id and file_type == "photo":
            sent = await bot.send_photo(settings.admin_user_id, file_id, caption=text, reply_markup=markup)
        elif file_id and file_type == "document":
            sent = await bot.send_document(settings.admin_user_id, file_id, caption=text, reply_markup=markup)
        else:
            sent = await bot.send_message(settings.admin_user_id, text, reply_markup=markup)
        await payments.finish_admin_notification(request.id, sent.message_id)
        return True
    except Exception:
        await payments.release_admin_notification(request.id)
        raise

from datetime import timezone
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.models import PaymentRequest
from app.payment_plans import plan_label, plan_price
from app.security import TokenSigner


def payment_generation(request: PaymentRequest) -> int:
    return int(request.created_at.replace(tzinfo=timezone.utc).timestamp() * 1_000_000)


def review_keyboard(request: PaymentRequest, signer: TokenSigner) -> InlineKeyboardMarkup:
    generation = payment_generation(request)
    approve_token = signer.sign_values("payment-yes", request.id, generation)
    reject_token = signer.sign_values("payment-no", request.id, generation)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Дать доступ", callback_data=f"payment:yes:{approve_token}"),
        InlineKeyboardButton(text="Не давать доступ", callback_data=f"payment:no:{reject_token}"),
    ]])


def review_text(request: PaymentRequest) -> str:
    name = escape(request.first_name or "Клиент")
    username = f" @{escape(request.username)}" if request.username else ""
    return (
        f"Оплата #{request.id}\n"
        f'<a href="tg://user?id={request.telegram_user_id}">{name}</a>{username}\n'
        f"Telegram ID: {request.telegram_user_id}\n"
        f"Тариф: {plan_label(request.plan)} · {plan_price(request.plan)} сом\n"
        f"Квартира: #{request.apartment_id}\n"
        "Клиент нажал «Я оплатил(а)»."
    )


async def notify_payment_admin(
    bot: Bot, settings: Settings, payments, signer: TokenSigner, request: PaymentRequest,
) -> None:
    if request.status != "pending":
        return
    if settings.admin_user_id <= 0:
        raise RuntimeError("Payment administrator is not configured")
    if not await payments.claim_admin_notification(request.id):
        return
    try:
        message = await bot.send_message(
            settings.admin_user_id, review_text(request),
            reply_markup=review_keyboard(request, signer), parse_mode="HTML",
        )
        await payments.finish_admin_notification(request.id, message.message_id)
    except Exception:
        await payments.release_admin_notification(request.id)
        raise

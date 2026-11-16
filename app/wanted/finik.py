"""Finik checkout and crash-safe publication for paid search requests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import secrets
import logging

from sqlalchemy import select, update
from app.finik import FinikClient, decode_private_key
from app.models import WantedAd, WantedPayment
from app.wanted.formatting import format_wanted_ad, format_wanted_public
from app.wanted.keyboards import wanted_public_keyboard

logger = logging.getLogger(__name__)


async def create_checkout(repository, settings, ad):
    payment_id = f'wanted-{ad.id}-{secrets.token_hex(12)}'
    now = datetime.now(timezone.utc)
    async with repository.sessions.begin() as session:
        locked = await session.scalar(select(WantedAd).where(WantedAd.id == ad.id).with_for_update())
        if locked is None or locked.status not in {'awaiting_payment', 'rejected'}:
            raise ValueError('Search request cannot be paid')
        existing = await session.scalar(select(WantedPayment).where(
            WantedPayment.wanted_ad_id == ad.id, WantedPayment.status == 'created',
            WantedPayment.created_at > now - timedelta(minutes=5)).order_by(WantedPayment.id.desc()).limit(1))
        if existing:
            if existing.payment_url:
                return existing.payment_url
            raise RuntimeError('Checkout is being prepared; try again later')
        payment = WantedPayment(wanted_ad_id=ad.id, provider_payment_id=payment_id,
                                status='created', created_at=now)
        session.add(payment)
    client = FinikClient(api_url=settings.finik_api_url, api_key=settings.finik_api_key,
        account_id=settings.finik_account_id, private_key_pem=decode_private_key(
            pem=settings.finik_private_key_pem, encoded=settings.finik_private_key_b64))
    checkout = await client.create_payment(payment_id=payment_id, amount=100,
        redirect_url=f"https://t.me/{settings.telegram_bot_username.lstrip('@')}",
        webhook_url=settings.require_public_base_url() + '/finik/webhook')
    async with repository.sessions.begin() as session:
        await session.execute(update(WantedPayment).where(WantedPayment.provider_payment_id == payment_id)
                              .values(payment_url=checkout.url))
    return checkout.url


async def apply_result(repository, payment_id, succeeded, amount):
    async with repository.sessions.begin() as session:
        payment = await session.scalar(select(WantedPayment).where(
            WantedPayment.provider_payment_id == payment_id).with_for_update())
        if payment is None:
            return 'missing', None
        if not succeeded:
            return 'not_succeeded', payment.wanted_ad_id
        try:
            # Finik checkouts were issued only at the original 100-som price.
            # Keep accepting those in-flight payments after the XPAY price change.
            matches = Decimal(str(amount)) == Decimal(100)
        except (InvalidOperation, ValueError, TypeError):
            matches = False
        if not matches:
            return 'amount_mismatch', payment.wanted_ad_id
        if payment.status == 'paid':
            return 'already_paid', payment.wanted_ad_id
        payment.status = 'paid'
        payment.paid_at = datetime.now(timezone.utc)
        payment.payment_url = None
        result = await session.execute(update(WantedAd).where(WantedAd.id == payment.wanted_ad_id,
            WantedAd.status.in_(['awaiting_payment', 'pending', 'rejected']))
            .values(status='paid', approved_at=payment.paid_at))
        return ('approved' if result.rowcount else 'already_handled'), payment.wanted_ad_id


async def publish_paid(repository, settings, bot, ad_id):
    # Reserve before sending. Never blindly replay an uncertain Telegram send.
    async with repository.sessions.begin() as session:
        claim = await session.execute(update(WantedAd).where(WantedAd.id == ad_id,
            WantedAd.status == 'paid').values(status='publishing'))
        if claim.rowcount != 1:
            return
    ad = await repository.get(ad_id)
    try:
        message = await bot.send_message(settings.telegram_group_id, format_wanted_public(ad),
            reply_markup=wanted_public_keyboard(ad, support_url=settings.support_bot_url))
    except Exception:
        # publishing is an ambiguous reservation: reconcile before retry.
        logger.exception("Paid search request %s needs publication reconciliation", ad_id)
        return
    await repository.mark_published(ad_id, message.message_id)
    try:
        await bot.send_message(ad.telegram_user_id,
            '✅ Оплата подтверждена. Ваша заявка опубликована в группе.\n\n' + format_wanted_ad(ad))
    except Exception:
        logger.exception("Could not notify customer of published search request %s", ad_id)

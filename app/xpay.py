"""XPAY dynamic checkout and provider-verified settlement."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, text

from app.models import PaymentRequest, WantedAd, WantedPayment, XPayAttempt
from app.payment_plans import PERSONAL_PLAN, WEEK_PLAN, WANTED_SEARCH_PRICE, plan_price
from app.payments.repository import PaymentRepository

UTC = timezone.utc
CONTACT_SERVICE = ('Бессрочный доступ Rentify', 'Оплата доступа Rentify навсегда')
WANTED_SERVICE = ('Заявка на поиск жилья Rentify', 'Оплата заявки на поиск Rentify')


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class XPayError(RuntimeError):
    pass


class XPayClient:
    def __init__(self, settings):
        self.settings = settings
        self.base = settings.xpay_api_url.rstrip('/')
        if urlsplit(self.base).scheme != 'https':
            raise XPayError('XPAY requires HTTPS')

    async def _token(self, client):
        response = await client.post(self.base + '/api/v1/developer/login', json={
            'client_id': self.settings.xpay_client_id,
            'client_secret': self.settings.xpay_client_secret,
        })
        response.raise_for_status()
        result = response.json()
        data = result.get('data') or {}
        service = next((s for s in data.get('service', []) if s.get('uuid') == self.settings.xpay_service_uuid), None)
        if result.get('status') != 'Success' or not data.get('access_token') or not service or service.get('currency_code') != 'KGS':
            raise XPayError('XPAY service unavailable')
        return data['access_token']

    async def create(self, *, amount, callback_url, check_url, payer_id, service_name, comments):
        if not service_name or len(service_name) > 32 or not comments or len(comments) > 32:
            raise XPayError('Invalid XPAY payment description')
        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._token(client)
            response = await client.post(self.base + '/api/v1/developer/qr/get',
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, json={
                    'uuid': self.settings.xpay_service_uuid, 'amount': amount * 100,
                    'type': 'dynamic', 'amount_change': False, 'payer_id': str(payer_id),
                    'service_name': service_name, 'comments': comments, 'callback_url': callback_url,
                    'check_url': check_url,
                })
            response.raise_for_status()
            payload = response.json()
            data = payload.get('data') or {}
            url = data.get('pay_link') or data.get('qr_code')
            parts = urlsplit(url or '')
            if (payload.get('status') != 'Success' or data.get('type') != 'dynamic'
                or data.get('amount_change') is not False or not data.get('qr_transaction_id')
                or parts.scheme != 'https' or parts.hostname not in {'pay.xpay.kg', 'devpay.xpay.kg'}
                or int(data.get('amount', -1)) != amount * 100
                or int(data.get('request_amount', -1)) != amount * 100):
                raise XPayError('Unexpected XPAY checkout response')
            return str(data['qr_transaction_id']), url

    async def status(self, provider_id):
        if not provider_id.isalnum() or len(provider_id) > 128:
            raise XPayError('Invalid XPAY transaction')
        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._token(client)
            response = await client.get(self.base + '/api/v1/developer/qr/dynamic/status/' + provider_id,
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
            response.raise_for_status()
            body = response.json()
            data = body.get('data') or {}
            if body.get('status') != 'Success' or data.get('qr_transaction_id') != provider_id:
                raise XPayError('Unexpected XPAY status response')
            return data


async def create_checkout(sessions, settings, *, request_id=None, wanted_ad_id=None, expected_plan=None):
    if not settings.xpay_auto_enabled or (request_id is None) == (wanted_ad_id is None):
        raise XPayError('XPAY is not configured')
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        if request_id is not None:
            request = await session.scalar(select(PaymentRequest).where(PaymentRequest.id == request_id).with_for_update())
            if request is None or request.plan not in (WEEK_PLAN, PERSONAL_PLAN) or request.status == 'approved':
                raise XPayError('Payment request unavailable')
            if expected_plan is not None and request.plan != expected_plan:
                raise XPayError('Checkout tariff changed; refresh the selected tariff')
            amount, payer_id = plan_price(request.plan), request.telegram_user_id
            # A customer can open several apartment cards at once. Serialize
            # these checkouts and reuse the same account-wide payment link.
            if session.bind.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(:namespace, :user_id)'),
                    {'namespace': 1481654617, 'user_id': int(payer_id) % 2147483647})
            other_ids = select(PaymentRequest.id).where(
                PaymentRequest.telegram_user_id == payer_id,
                PaymentRequest.plan == request.plan,
                PaymentRequest.status.in_(['prepared', 'awaiting_receipt']))
            legacy = await session.scalar(select(PaymentRequest.id).where(
                PaymentRequest.telegram_user_id == payer_id,
                PaymentRequest.plan == request.plan,
                PaymentRequest.status.in_(['prepared', 'awaiting_receipt']),
                PaymentRequest.provider_payment_url.is_not(None),
                PaymentRequest.provider_status.like('waiting:%'),
                PaymentRequest.provider_status != 'waiting:xpay',
                PaymentRequest.created_at > now - timedelta(minutes=5),
            ).limit(1))
            if legacy is not None:
                raise XPayError('Previous checkout is still active')
            predicate = (XPayAttempt.request_id.in_(other_ids) & ((XPayAttempt.checkout_plan == request.plan) | ((XPayAttempt.checkout_plan.is_(None)) & (request.plan == WEEK_PLAN))))
        else:
            request = await session.scalar(select(WantedAd).where(WantedAd.id == wanted_ad_id).with_for_update())
            if request is None or request.status not in {'awaiting_payment', 'rejected'}:
                raise XPayError('Search request unavailable')
            amount, payer_id = WANTED_SEARCH_PRICE, request.telegram_user_id
            predicate = XPayAttempt.wanted_ad_id == wanted_ad_id
        active = await session.scalar(select(XPayAttempt).where(predicate, XPayAttempt.expires_at > now,
            XPayAttempt.status.in_(['creating', 'waiting'])).order_by(XPayAttempt.id.desc()).limit(1))
        if active:
            if active.expected_amount != amount:
                raise XPayError('Previous checkout is still active at a different price')
            if active.payment_url:
                if request_id is not None:
                    request.created_at = active.created_at
                return active.payment_url, utc(active.expires_at)
            raise XPayError('Checkout is being prepared')
        previous = await session.scalar(select(XPayAttempt).where(predicate,
            XPayAttempt.provider_id.is_not(None), XPayAttempt.status == 'waiting'
        ).order_by(XPayAttempt.id.desc()).limit(1))
        if previous:
            # A bank may have prepared or started the previous payment before
            # the link expired. Do not create another payable QR while that
            # transaction can still settle, even if its callback is delayed.
            previous_status = (await XPayClient(settings).status(previous.provider_id)).get('pay_status')
            if previous_status not in {'WAITING', 'ERROR', 'CANCELED'}:
                raise XPayError('Previous payment is still being confirmed')
        attempt = XPayAttempt(token=secrets.token_urlsafe(32), request_id=request_id,
            wanted_ad_id=wanted_ad_id, expected_amount=amount,
            checkout_plan=request.plan if request_id is not None else None, status='creating',
            created_at=now, expires_at=now + timedelta(minutes=5))
        session.add(attempt)
        await session.flush()
        attempt_id, callback_token, expires = attempt.id, attempt.token, attempt.expires_at
    base = settings.require_public_base_url()
    try:
        service_name, comments = CONTACT_SERVICE if request_id is not None else WANTED_SERVICE
        if request_id is not None and request.plan == PERSONAL_PLAN:
            service_name, comments = 'Под вас — Rentify', 'Подбор квартир на 30 дней'
        provider_id, url = await XPayClient(settings).create(amount=amount, payer_id=payer_id,
            service_name=service_name, comments=comments,
            callback_url=base + '/xpay/webhook/' + callback_token,
            check_url=base + '/xpay/check/' + callback_token)
    except Exception:
        async with sessions.begin() as session:
            attempt = await session.get(XPayAttempt, attempt_id, with_for_update=True)
            if attempt and attempt.status == 'creating': attempt.status = 'failed'
        raise
    async with sessions.begin() as session:
        attempt = await session.get(XPayAttempt, attempt_id, with_for_update=True)
        if not attempt or attempt.status != 'creating':
            raise XPayError('Checkout reservation changed')
        attempt.provider_id, attempt.payment_url, attempt.status = provider_id, url, 'waiting'
        if request_id is not None:
            request = await session.get(PaymentRequest, request_id, with_for_update=True)
            if not request or request.status == 'approved' or request.plan != attempt.checkout_plan:
                raise XPayError('Payment request changed')
            request.provider_payment_id = provider_id
            request.provider_expected_amount = amount
            request.provider_payment_url = url
            request.provider_status = 'waiting:xpay'
            request.created_at = now
        else:
            session.add(WantedPayment(wanted_ad_id=wanted_ad_id, provider_payment_id=provider_id,
                status='created', payment_url=url, created_at=now))
    return url, utc(expires)


async def reconcile(sessions, settings, token):
    async with sessions.begin() as session:
        attempt = await session.scalar(select(XPayAttempt).where(XPayAttempt.token == token))
        if not attempt or not attempt.provider_id:
            return 'missing', None, None
        provider_id, expected = attempt.provider_id, attempt.expected_amount
        attempt.last_checked_at = datetime.now(UTC)
    data = await XPayClient(settings).status(provider_id)
    if data.get('pay_status') != 'COMPLETED':
        if data.get('pay_status') in {'ERROR', 'CANCELED', 'WAITING'}:
            async with sessions.begin() as session:
                attempt = await session.scalar(select(XPayAttempt).where(XPayAttempt.token == token).with_for_update())
                stale_waiting = bool(attempt and data.get('pay_status') == 'WAITING'
                    and utc(attempt.expires_at) < datetime.now(UTC) - timedelta(days=2))
                if attempt and attempt.status == 'waiting' and (data.get('pay_status') != 'WAITING' or stale_waiting):
                    attempt.status, attempt.payment_url = 'closed', None
        return 'waiting', None, None
    try:
        # This merchant's production status response uses tyiyn: 39900 for
        # 399 som. Do not infer units from JSON number/string formatting.
        def matches(value):
            return Decimal(str(value)) == Decimal(expected * 100)
        if not matches(data.get('amount')) or not matches(data.get('request_amount')):
            return 'amount_mismatch', None, None
    except (InvalidOperation, ValueError, TypeError):
        return 'amount_mismatch', None, None
    if not data.get('transaction_uuid'):
        return 'unverified', None, None
    async with sessions.begin() as session:
        attempt = await session.scalar(select(XPayAttempt).where(XPayAttempt.token == token).with_for_update())
        if not attempt or attempt.provider_id != provider_id:
            return 'missing', None, None
        if attempt.status == 'paid':
            return 'already_paid', attempt.request_id, attempt.wanted_ad_id
        if attempt.request_id:
            request = await session.get(PaymentRequest, attempt.request_id, with_for_update=True)
            if request is None: return 'missing', None, None
            if request.status != 'approved' or request.plan != (attempt.checkout_plan or WEEK_PLAN):
                request.plan = attempt.checkout_plan or WEEK_PLAN
                request.provider_payment_id = provider_id
                request.provider_expected_amount = expected
                request.provider_status = 'succeeded'
                PaymentRepository._grant_access(session, request, now=datetime.now(UTC), approved_by=None)
                await PaymentRepository._close_sibling_requests(session, request, now=datetime.now(UTC), approved_by=None)
                outcome = 'approved'
            else:
                outcome = 'already_paid'
            attempt.status, attempt.payment_url = 'paid', None
            return outcome, attempt.request_id, None
        wanted = await session.get(WantedAd, attempt.wanted_ad_id, with_for_update=True)
        payment = await session.scalar(select(WantedPayment).where(WantedPayment.provider_payment_id == provider_id).with_for_update())
        if not wanted or not payment: return 'missing', None, None
        payment.status, payment.paid_at, payment.payment_url = 'paid', datetime.now(UTC), None
        outcome = 'already_paid'
        if wanted.status in {'awaiting_payment', 'pending', 'rejected'}:
            wanted.status, wanted.approved_at = 'paid', datetime.now(UTC)
            outcome = 'approved'
        attempt.status, attempt.payment_url = 'paid', None
        return outcome, None, attempt.wanted_ad_id

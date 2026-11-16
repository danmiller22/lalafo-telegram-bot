"""Encrypted XPAY configuration shared with the hosted payment service."""
import base64
import hashlib
import json
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url
from app.database import create_engine_and_session
from app.models import XPayConfig


def cipher(database_url):
    password = make_url(database_url).password
    if not password:
        raise RuntimeError('XPAY vault requires a private database credential')
    key = hashlib.sha256(('xpay-config-v1:' + password).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_credentials(database_url, credentials):
    return cipher(database_url).encrypt(json.dumps(credentials).encode()).decode()


async def load_stored_credentials(settings):
    if settings.xpay_client_id and settings.xpay_client_secret and settings.xpay_service_uuid:
        return True
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        async with sessions() as session:
            row = await session.get(XPayConfig, 1)
            if row is None:
                return False
            data = json.loads(cipher(settings.database_url).decrypt(row.encrypted_credentials.encode()))
        settings.xpay_client_id = data['client_id']
        settings.xpay_client_secret = data['client_secret']
        settings.xpay_service_uuid = data['uuid']
        return True
    finally:
        await engine.dispose()

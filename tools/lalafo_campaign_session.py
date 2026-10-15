"""Transfer the operator's existing session using an encrypted, scoped envelope."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import get_settings
from app.database import create_engine_and_session
from app.models import LalafoAutoReplyMeta

KEY_NAME = os.getenv('CAMPAIGN_SESSION_KEY_NAME', 'requested_campaign_expiry_key_20261012')


def storage_cipher(secret):
    if not secret:
        raise RuntimeError('Session encryption secret is missing')
    key = hashlib.sha256(('campaign-session-transfer:' + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


async def private_key(*, create=False, key_name=None):
    chosen_key = key_name or KEY_NAME
    settings = get_settings()
    cipher = storage_cipher(settings.require_callback_secret())
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        async with sessions.begin() as session:
            row = await session.get(LalafoAutoReplyMeta, chosen_key, with_for_update=True)
            if row is None:
                if not create:
                    raise RuntimeError('Campaign session transfer key has not been prepared')
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                session.add(LalafoAutoReplyMeta(key=chosen_key, value=cipher.encrypt(pem).decode()))
                return key
            return serialization.load_pem_private_key(cipher.decrypt(row.value.encode()), password=None)
    finally:
        await engine.dispose()


def seal_session(public_key_pem, session):
    public_key = serialization.load_pem_public_key(public_key_pem)
    content_key = Fernet.generate_key()
    ciphertext = Fernet(content_key).encrypt(json.dumps(session).encode()).decode()
    wrapped = public_key.encrypt(content_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return {'wrapped_key': base64.b64encode(wrapped).decode(), 'ciphertext': ciphertext}


def open_session(private_key, envelope):
    content_key = private_key.decrypt(base64.b64decode(envelope['wrapped_key']), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return json.loads(Fernet(content_key).decrypt(envelope['ciphertext'].encode()))


async def load_session(envelope, *, key_name=None):
    return open_session(await private_key(key_name=key_name), envelope)


async def cutoff_completed(*, key_name=None):
    chosen_key = key_name or KEY_NAME
    engine, sessions = create_engine_and_session(get_settings().database_url)
    try:
        async with sessions() as session:
            return await session.get(LalafoAutoReplyMeta, chosen_key + '_completed') is not None
    finally:
        await engine.dispose()


async def retire_session_key(*, record_completion=False, key_name=None):
    chosen_key = key_name or KEY_NAME
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        async with sessions.begin() as session:
            row = await session.get(LalafoAutoReplyMeta, chosen_key)
            if row is not None:
                await session.delete(row)
            if record_completion and await session.get(LalafoAutoReplyMeta, chosen_key + '_completed') is None:
                session.add(LalafoAutoReplyMeta(key=chosen_key + '_completed', value='confirmed'))
    finally:
        await engine.dispose()


async def prepare():
    key = await private_key(create=True)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    print('CAMPAIGN_RSA_PUBLIC_KEY=' + base64.b64encode(pem).decode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-key', action='store_true')
    args = parser.parse_args()
    if args.prepare_key:
        asyncio.run(prepare())

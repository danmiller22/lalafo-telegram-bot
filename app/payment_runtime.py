"""Encrypted operator configuration; loaded once, never on the payment hot path."""
import base64
import hashlib
import json

from cryptography.fernet import Fernet
from app.models import PaymentRuntimeConfig

CONFIG_KEY = "finik_configuration"
TRANSFER_KEY = "finik_transfer_private_key"


def cipher(secret):
    if not secret or secret == "change-me-in-production":
        raise ValueError("Payment configuration encryption is not configured")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("payment-runtime:" + secret).encode()).digest()))


async def read_config(sessions, secret):
    async with sessions() as session:
        row = await session.get(PaymentRuntimeConfig, CONFIG_KEY)
        return json.loads(cipher(secret).decrypt(row.value.encode())) if row else None


async def write_config(sessions, secret, config):
    encrypted = cipher(secret).encrypt(json.dumps(config).encode()).decode()
    async with sessions.begin() as session:
        row = await session.get(PaymentRuntimeConfig, CONFIG_KEY, with_for_update=True)
        if row is None:
            session.add(PaymentRuntimeConfig(key=CONFIG_KEY, value=encrypted))
        else:
            row.value = encrypted


async def load_enabled_config(settings, sessions):
    config = await read_config(sessions, settings.require_callback_secret())
    if config and config.get("enabled"):
        settings.finik_api_key = config["api_key"]
        settings.finik_api_url = config.get("api_url", settings.finik_api_url)
        settings.finik_automatic_access_enabled = True

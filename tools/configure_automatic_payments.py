"""Transfer an API key encrypted; never print credentials or perform a charge."""
import argparse
import asyncio
import base64
import json
import os
import urllib.request
import urllib.error

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from app.config import get_settings
from app.database import create_engine_and_session, init_db
from app.models import PaymentRuntimeConfig
from app.payment_runtime import TRANSFER_KEY, cipher, write_config
from tools.lalafo_campaign_session import open_session


async def run(operation):
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        await init_db(engine)
        secret = settings.require_callback_secret()
        encryption = cipher(secret)
        if operation == "prepare":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            async with sessions.begin() as s:
                row = await s.get(PaymentRuntimeConfig, TRANSFER_KEY, with_for_update=True)
                value = encryption.encrypt(pem).decode()
                if row:
                    row.value = value
                else:
                    s.add(PaymentRuntimeConfig(key=TRANSFER_KEY, value=value))
            public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            print("PAYMENT_TRANSFER_PUBLIC_KEY="+base64.b64encode(public).decode())
        elif operation == "stage":
            async with sessions.begin() as s:
                row = await s.get(PaymentRuntimeConfig, TRANSFER_KEY, with_for_update=True)
                if row is None:
                    raise RuntimeError("Prepare the payment transfer first")
                key = serialization.load_pem_private_key(encryption.decrypt(row.value.encode()), password=None)
                config = open_session(key, json.loads(os.environ["ENCRYPTED_PAYMENT_CONFIGURATION"]))
                if not isinstance(config.get("api_key"), str) or len(config["api_key"]) < 20:
                    raise ValueError("Invalid payment key")
                await s.delete(row)
            await write_config(sessions, secret, {"api_key":config["api_key"], "enabled":False})
            print("PAYMENT_SETUP=encrypted_key_staged")
        elif operation == "activate":
            request = urllib.request.Request(
                "https://statutory-mallissa-2danmiller-f1c1b08d.koyeb.app/internal/payments/activate",
                data=b"", headers={"Authorization":"Bearer "+secret}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    print("PAYMENT_SETUP="+response.read().decode())
            except urllib.error.HTTPError as exc:
                print("PAYMENT_SETUP_FAILED_HTTP="+str(exc.code))
                print("PAYMENT_SETUP_DETAIL="+exc.read().decode())
                raise SystemExit(1) from None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare","stage","activate"])
    asyncio.run(run(parser.parse_args().operation))

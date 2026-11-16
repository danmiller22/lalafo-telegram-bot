"""Store the user's XPAY credentials encrypted without printing them."""
import asyncio
import json
from pathlib import Path
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import XPayConfig
from app.xpay import XPayClient
from app.xpay_vault import encrypt_credentials
from tools.lalafo_campaign_session import load_session

async def main():
    settings = get_settings()
    envelope = json.loads(Path('tools/xpay-bootstrap.sealed.json').read_text())
    credentials = await load_session(envelope['sealed_card'], key_name=envelope['key_name'])
    if credentials['uuid'] != '02ff58f9-687d-4066-b07a-d854fcc2b550':
        raise RuntimeError('Unexpected XPAY service')
    settings.xpay_client_id = credentials['client_id']
    settings.xpay_client_secret = credentials['client_secret']
    settings.xpay_service_uuid = credentials['uuid']
    import httpx
    async with httpx.AsyncClient(timeout=20) as http:
        await XPayClient(settings)._token(http)
    protected = encrypt_credentials(settings.database_url, credentials)
    engine, sessions = create_engine_and_session(settings.database_url)
    try:
        async with sessions.begin() as session:
            row = await session.get(XPayConfig, 1, with_for_update=True)
            if row is None:
                session.add(XPayConfig(id=1, encrypted_credentials=protected))
            else:
                row.encrypted_credentials = protected
    finally:
        await engine.dispose()
    print('XPAY_CONFIGURATION_ENCRYPTED_AND_VALIDATED')

if __name__ == '__main__':
    asyncio.run(main())

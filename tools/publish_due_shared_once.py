"""Dispatch one due shared offer using the normal quota, gap and send guards."""
import asyncio,json,logging
from datetime import datetime,timezone
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment
from app.inventory import InventoryRepository
from app.payments.repository import ApartmentRepository
from scripts.publish_inventory import _publish_item

async def main():
    settings=get_settings();engine,sessions=create_engine_and_session(settings.database_url)
    try:
        async with sessions() as s:
            ids=list((await s.scalars(select(Apartment.id).where(Apartment.rooms=='shared'))).all())
        inventory=InventoryRepository(sessions)
        item=await inventory.claim_due(now=datetime.now(timezone.utc),only_apartment_ids=ids)
        if item is None:
            print('SHARED_DISPATCH '+json.dumps({'status':'not_due'}));return
        result=await _publish_item(settings,sessions,inventory,ApartmentRepository(sessions),item)
        print('SHARED_DISPATCH '+json.dumps({'status':'published' if result is not None else 'handled_without_publication'}),flush=True)
    finally:await engine.dispose()
if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

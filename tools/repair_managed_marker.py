"""Repair one marker after validating its live public card; preserve its expiry."""
import asyncio,json
from datetime import datetime,timezone
from app.config import get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoClient
from app.managed_cards import PREFIX,refresh_managed_card
from app.models import Apartment,LalafoAutoReplyMeta,RequestedCardPromotion
from scripts.scrape_publish import apartment_to_ad
async def main():
 settings=get_settings();engine,sessions=create_engine_and_session(settings.database_url)
 try:
  async with sessions() as s:
   row=await s.get(LalafoAutoReplyMeta,PREFIX+'2817');binding=json.loads(row.value)
   assert binding['profile_id']==18194628 and binding['ad_id']==117338212
   apartment=await s.get(Apartment,2817)
   assert apartment.active
  async with LalafoClient(timeout=15,max_retries=0,proxy_url=settings.lalafo_proxy_url) as client:
   await refresh_managed_card(client,binding,apartment_to_ad(apartment))
  async with sessions.begin() as s:
   row=await s.get(LalafoAutoReplyMeta,PREFIX+'2817',with_for_update=True)
   promotion=await s.get(RequestedCardPromotion,2817)
   assert promotion.expires_at>datetime.now(timezone.utc)
   current=json.loads(row.value);assert current['ad_id']==117338212 and current['profile_id']==18194628
   current['enabled']=True;row.value=json.dumps(current)
   print('MANAGED_BINDING_REPAIRED='+json.dumps({'ad_id':117338212,'expires_at':promotion.expires_at.isoformat()}),flush=True)
 finally:await engine.dispose()
asyncio.run(main())

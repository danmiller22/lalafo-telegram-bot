"""Verify and settle one explicitly identified XPAY payment once."""
import asyncio,json
from types import SimpleNamespace
from aiogram import Bot
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import PaymentRequest,XPayAttempt
from app.payments.repository import PaymentRepository
from app.xpay import reconcile
from app.xpay_vault import load_stored_credentials
from app.web import _deliver_verified_finik_access

class TrackingBot:
 def __init__(self,bot,user_id,admin_id):self.bot,self.user_id,self.admin_id,self.receipts=bot,user_id,admin_id,[]
 def __getattr__(self,name):
  original=getattr(self.bot,name)
  if name not in ('send_message','send_photo','send_media_group'):return original
  async def send(*args,**kwargs):
   result=await original(*args,**kwargs)
   dest=kwargs.get('chat_id',args[0] if args else None)
   self.receipts.append({'destination':'buyer' if dest==self.user_id else 'admin' if dest==self.admin_id else 'other','method':name,'confirmed':bool(result)})
   return result
  return send

async def main():
 settings=get_settings();assert settings.admin_user_id
 await load_stored_credentials(settings)
 engine,sessions=create_engine_and_session(settings.database_url);bot=Bot(settings.require_bot_token())
 try:
  async with sessions() as session:
   a=await session.get(XPayAttempt,1); assert a and a.request_id==2725 and a.expected_amount==399
   p=await session.get(PaymentRequest,a.request_id);assert p and p.telegram_user_id==605775820
   token=a.token;uid=p.telegram_user_id
  result,request_id,wanted_id=await reconcile(sessions,settings,token)
  tracker=TrackingBot(bot,uid,settings.admin_user_id)
  if result=='approved':
   payments=PaymentRepository(sessions)
   runtime=SimpleNamespace(bot=tracker,workflow_data={'payments':payments})
   await _deliver_verified_finik_access(runtime,settings,request_id)
  async with sessions() as session:
   p=await session.get(PaymentRequest,2725)
   print('XPAY_RECONCILIATION='+json.dumps({'result':result,'request_status':p.status,'admin_notified':bool(p.provider_admin_message_id and p.provider_admin_message_id>0),'delivery_receipts':tracker.receipts}),flush=True)
 finally:
  await bot.session.close();await engine.dispose()
asyncio.run(main())

"""Encrypted read-only XPAY reconciliation and notification diagnostics."""
import asyncio, json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import PaymentHistory,PaymentRequest,XPayAttempt,WantedAd
from app.xpay import XPayClient
from app.xpay_vault import load_stored_credentials
from tools.lalafo_campaign_session import seal_session

def iso(v):return v.isoformat() if v else None
async def main():
 settings=get_settings(); await load_stored_credentials(settings)
 engine,sessions=create_engine_and_session(settings.database_url);out={'checked_utc':iso(datetime.now(timezone.utc)),'attempts':[],'recent_payments':[]}
 try:
  async with sessions() as session:
   since=datetime.now(timezone.utc)-timedelta(hours=24)
   attempts=(await session.scalars(select(XPayAttempt).where(XPayAttempt.created_at>=since).order_by(XPayAttempt.id.desc()).limit(100))).all()
   for a in attempts:
    item={'id':a.id,'type':'contacts' if a.request_id else 'wanted','created':iso(a.created_at),'local_status':a.status,'amount':a.expected_amount,'provider_id_present':bool(a.provider_id),'last_checked':iso(a.last_checked_at)}
    if a.request_id:
     p=await session.get(PaymentRequest,a.request_id)
     if p:item.update(request_id=p.id,user_id=p.telegram_user_id,username=p.username,first_name=p.first_name,request_status=p.status,approved_at=iso(p.approved_at),admin_message_id=p.provider_admin_message_id)
    else:
     p=await session.get(WantedAd,a.wanted_ad_id)
     if p:item.update(wanted_id=p.id,user_id=p.telegram_user_id,username=p.username,first_name=p.first_name,request_status=p.status,published=bool(p.telegram_message_id))
    if a.provider_id:
     try:
      status=await XPayClient(settings).status(a.provider_id)
      item.update(provider_status=status.get('pay_status'),provider_amount=status.get('amount'),provider_request_amount=status.get('request_amount'),executed= status.get('executed_time'))
     except Exception as e:item['provider_error']=type(e).__name__
    out['attempts'].append(item)
   payments=(await session.scalars(select(PaymentHistory).where(PaymentHistory.paid_at>=since).order_by(PaymentHistory.paid_at.desc()).limit(100))).all()
   for p in payments:
    out['recent_payments'].append({'user_id':p.telegram_user_id,'username':p.username,'first_name':p.first_name,'amount':p.amount,'paid_at':iso(p.paid_at),'request_id':p.payment_request_id,'plan':p.plan})
  print('XPAY_DIAGNOSTICS_ENCRYPTED='+json.dumps(seal_session(Path('tools/xpay-diagnostics-public.pem').read_bytes(),out)),flush=True)
 finally:await engine.dispose()
asyncio.run(main())

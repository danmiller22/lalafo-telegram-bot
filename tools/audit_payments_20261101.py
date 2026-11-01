"""Read-only payment-chain audit with encrypted aggregate output."""
import asyncio,json,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from sqlalchemy import select,func,text
from app.database import create_engine_and_session
from app.models import CustomerFunnelEvent,PaymentRequest,PaymentHistory
from tools.revenue_diagnostics import _period_summary,_funnel_sources
from tools.lalafo_campaign_session import seal_session
async def main():
 now=datetime.now(timezone.utc);day=now.astimezone(ZoneInfo('Asia/Bishkek')).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
 engine,sessions=create_engine_and_session(os.environ['DATABASE_URL'])
 try:
  async with sessions() as s:
   await s.execute(text('SET TRANSACTION READ ONLY'))
   out={'generated_at':now.isoformat(),'read_only':True,'periods':{}}
   windows={'today':(day,now),'yesterday_same_time':(day-timedelta(days=1),now-timedelta(days=1)),'last_1h':(now-timedelta(hours=1),now),'last_3h':(now-timedelta(hours=3),now),'last_6h':(now-timedelta(hours=6),now),'last_24h':(now-timedelta(hours=24),now),'previous_24h':(now-timedelta(hours=48),now-timedelta(hours=24)),'last_7d':(now-timedelta(days=7),now),'previous_7d':(now-timedelta(days=14),now-timedelta(days=7))}
   for key,(start,end) in windows.items():out['periods'][key]=await _period_summary(s,start,end)
   out['sources_last24h']=await _funnel_sources(s,now-timedelta(hours=24),now)
   rows=(await s.execute(select(PaymentRequest.plan,PaymentRequest.status,PaymentRequest.provider_status,func.count()).where(PaymentRequest.created_at>=now-timedelta(hours=24)).group_by(PaymentRequest.plan,PaymentRequest.status,PaymentRequest.provider_status))).all()
   out['checkout_statuses_last24h']=[dict(plan=p,status=st,provider_status=pr,count=n) for p,st,pr,n in rows]
   row=(await s.execute(select(func.count(),func.min(PaymentRequest.payment_claimed_at),func.count().filter(PaymentRequest.admin_message_id.is_(None))).where(PaymentRequest.status=='pending',PaymentRequest.payment_claimed_at.is_not(None)))).one()
   out['claimed_pending']={'count':row[0],'oldest_claim_at':str(row[1]),'missing_admin_notification':row[2]}
   out['last_confirmed_payment_at']=str(await s.scalar(select(func.max(PaymentHistory.paid_at))))
   out['last_checkout_at']=str(await s.scalar(select(func.max(CustomerFunnelEvent.created_at)).where(CustomerFunnelEvent.event_name=='checkout_started')))
   out['hourly']=[]
   for i in range(12):
    end=now-timedelta(hours=i);start=end-timedelta(hours=1);out['hourly'].append(await _period_summary(s,start,end))
   print('SEALED_PAYMENT_AUDIT='+json.dumps(seal_session(Path('tools/payment_audit_transport_public.pem').read_bytes(),out)),flush=True)
 finally:await engine.dispose()
asyncio.run(main())

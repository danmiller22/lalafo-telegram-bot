"""Read-only business analysis; all output encrypted for local inspection."""
import asyncio,json,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from collections import Counter,defaultdict
from zoneinfo import ZoneInfo
from sqlalchemy import select,text,func
from app.database import create_engine_and_session
from app.models import PaymentHistory,CustomerFunnelEvent,PaymentRequest,Apartment,ApartmentSearchProfile,ApartmentInventoryQueue
from tools.revenue_diagnostics import _period_summary,_revenue_segments,_funnel_sources
from tools.lalafo_campaign_session import seal_session

def utc(t):return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)

async def main():
 now=datetime.now(timezone.utc);zone=ZoneInfo('Asia/Bishkek');local=now.astimezone(zone);midnight=local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
 engine,sessions=create_engine_and_session(os.environ['DATABASE_URL'])
 try:
  async with sessions() as s:
   await s.execute(text('SET TRANSACTION READ ONLY'))
   pays=list((await s.execute(select(PaymentHistory.telegram_user_id,PaymentHistory.paid_at,PaymentHistory.amount,PaymentHistory.plan,PaymentHistory.apartment_id))).all())
   first={}
   for p in pays:first[p.telegram_user_id]=min(first.get(p.telegram_user_id,utc(p.paid_at)),utc(p.paid_at))
   coverage=(await s.execute(select(func.min(CustomerFunnelEvent.created_at),func.max(CustomerFunnelEvent.created_at)))).one()
   result={'generated_at':now.isoformat(),'business_timezone':'Asia/Bishkek','read_only':True,
     'coverage':{'first_ledger_payment':min((utc(p.paid_at) for p in pays),default=now).isoformat(),'last_ledger_payment':max((utc(p.paid_at) for p in pays),default=now).isoformat(),'ledger_count':len(pays),'first_funnel_event':str(coverage[0]),'last_funnel_event':str(coverage[1])}}
   result['weeks']=[]
   for i in range(4):
    end=now-timedelta(days=7*i);start=end-timedelta(days=7)
    row=await _period_summary(s,start,end)
    period=[p for p in pays if start<=utc(p.paid_at)<end]
    new={p.telegram_user_id for p in period if first[p.telegram_user_id]>=start}
    repeat={p.telegram_user_id for p in period if first[p.telegram_user_id]<start}
    row.update(new_payers=len(new),returning_payers=len(repeat),plans=dict(Counter(p.plan for p in period)),amounts=dict(Counter(p.amount for p in period)),sources=await _funnel_sources(s,start,end),segments=await _revenue_segments(s,start,end))
    opened=set((await s.scalars(select(CustomerFunnelEvent.telegram_user_id).where(CustomerFunnelEvent.event_name=='card_opened',CustomerFunnelEvent.created_at>=start,CustomerFunnelEvent.created_at<end))).all())
    row['visitors_with_prior_payment']=sum(uid in first and first[uid]<start for uid in opened)
    row['paid_users_who_opened_in_period']=len(opened & {p.telegram_user_id for p in period})
    result['weeks'].append(row)
   result['days']=[]
   for i in range(22):
    start=midnight-timedelta(days=i);end=min(start+timedelta(days=1),now)
    row=await _period_summary(s,start,end);row['date']=start.astimezone(zone).date().isoformat();result['days'].append(row)
   events=(await s.execute(select(CustomerFunnelEvent.event_name,func.min(CustomerFunnelEvent.created_at),func.count()).group_by(CustomerFunnelEvent.event_name))).all()
   result['event_coverage']={name:{'first':str(at),'count':n} for name,at,n in events}
   requests=list((await s.scalars(select(PaymentRequest).where(PaymentRequest.payment_claimed_at.is_not(None)))).all())
   result['approval_backlog']={'pending':sum(r.status=='pending' for r in requests),'missing_admin_notification':sum(r.status=='pending' and r.admin_message_id is None for r in requests)}
   latencies=sorted((utc(r.approved_at)-utc(r.payment_claimed_at)).total_seconds()/60 for r in requests if r.approved_at and r.payment_claimed_at and utc(r.approved_at)>=utc(r.payment_claimed_at))
   result['approval_minutes']={'sample':len(latencies),'median':latencies[len(latencies)//2] if latencies else None,'p90':latencies[min(len(latencies)-1,int(len(latencies)*.9))] if latencies else None}
   profiles=list((await s.scalars(select(ApartmentSearchProfile))).all())
   result['search_demand']={'profiles':len(profiles),'budget_bands':dict(Counter('<=25000' if p.max_budget<=25000 else '25001-35000' if p.max_budget<=35000 else '35001-45000' if p.max_budget<=45000 else '>45000' for p in profiles)), 'rooms':dict(Counter(room for p in profiles for room in p.rooms)),'districts':dict(Counter(d for p in profiles for d in p.districts))}
   stock=list((await s.scalars(select(Apartment).where(Apartment.active.is_(True),Apartment.last_seen_at>=now-timedelta(hours=48)))).all())
   result['stock']={'fresh_active':len(stock),'statuses':dict(Counter(a.publication_status for a in stock)),'sellers':dict(Counter(a.seller_type for a in stock)),'price_bands':dict(Counter('<=25000' if a.price<=25000 else '25001-35000' if a.price<=35000 else '35001-45000' if a.price<=45000 else '>45000' for a in stock)),'source_domains':dict(Counter('telegram' if a.source_url.startswith('https://t.me/') else 'lalafo' if 'lalafo.kg/' in a.source_url else 'other' for a in stock))}
   result['queue_status']=dict((await s.execute(select(ApartmentInventoryQueue.status,func.count()).group_by(ApartmentInventoryQueue.status))).all())
   # Segment by existing card attributes; no customer identifiers are emitted.
   for i,row in enumerate(result['weeks'][:2]):
    end=now-timedelta(days=7*i);start=end-timedelta(days=7)
    result.setdefault('card_segments',[]).append([{'seller':seller,'price_band':band,'opens':opens,'visitors':visitors} for seller,band,opens,visitors in (await s.execute(text("SELECT coalesce(a.seller_type,'unknown'), CASE WHEN a.price<=35000 THEN '<=35000' ELSE '>35000' END, count(*), count(distinct e.telegram_user_id) FROM customer_funnel_events e JOIN apartments a ON a.id=e.apartment_id WHERE e.event_name='card_opened' AND e.created_at>=:start AND e.created_at<:end GROUP BY 1,2"),{'start':start,'end':end})).all()])
   print('SEALED_PAYMENT_AUDIT='+json.dumps(seal_session(Path('tools/payment_audit_transport_public.pem').read_bytes(),result)),flush=True)
 finally:await engine.dispose()

asyncio.run(main())

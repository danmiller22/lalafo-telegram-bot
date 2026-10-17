"""Read aggregate funnel and confirmed payment history; never expose customers."""
import json
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select,func
from app.models import CustomerFunnelEvent,PaymentRequest,PaymentHistory,Apartment

async def report(sessions):
 now=datetime.now(timezone.utc);local=now.astimezone(ZoneInfo('Asia/Bishkek'));midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
 async with sessions() as s:
  for days in range(7):
   start=(midnight-timedelta(days=days)).astimezone(timezone.utc);end=start+(now-midnight.astimezone(timezone.utc))
   payments=(await s.execute(select(func.count(),func.coalesce(func.sum(PaymentHistory.amount),0)).where(PaymentHistory.paid_at>=start,PaymentHistory.paid_at<end))).one()
   events=(await s.execute(select(CustomerFunnelEvent.event_name,func.count(),func.count(func.distinct(CustomerFunnelEvent.telegram_user_id))).where(CustomerFunnelEvent.created_at>=start,CustomerFunnelEvent.created_at<end).group_by(CustomerFunnelEvent.event_name))).all()
   requests=(await s.execute(select(PaymentRequest.status,func.count()).where(PaymentRequest.created_at>=start,PaymentRequest.created_at<end).group_by(PaymentRequest.status))).all()
   print('REVENUE_DIAGNOSTICS='+json.dumps({'date':(midnight-timedelta(days=days)).date().isoformat(),'same_local_cutoff':local.strftime('%H:%M'),'confirmed_payments':payments[0],'confirmed_revenue_som':payments[1],'events':{name:{'events':n,'users':u} for name,n,u in events},'new_requests_current_status':dict(requests)}),flush=True)
  for hour in range(24):
   end=now-timedelta(hours=hour);start=end-timedelta(hours=1)
   paid=(await s.execute(select(func.count(),func.coalesce(func.sum(PaymentHistory.amount),0)).where(PaymentHistory.paid_at>=start,PaymentHistory.paid_at<end))).one()
   events=(await s.execute(select(CustomerFunnelEvent.event_name,func.count()).where(CustomerFunnelEvent.created_at>=start,CustomerFunnelEvent.created_at<end).group_by(CustomerFunnelEvent.event_name))).all()
   print('REVENUE_HOUR='+json.dumps({'start':start.isoformat(),'confirmed_payments':paid[0],'revenue_som':paid[1],'events':dict(events)}),flush=True)
  pending=(await s.execute(select(func.count(),func.min(PaymentRequest.payment_claimed_at),func.count().filter(PaymentRequest.admin_message_id.is_(None))).where(PaymentRequest.status=='pending',PaymentRequest.payment_claimed_at.is_not(None)))).one()
  print('PAYMENT_REVIEW_BACKLOG='+json.dumps({'claimed_pending':pending[0],'oldest_claim_at':pending[1].isoformat() if pending[1] else None,'missing_admin_notification':pending[2]}),flush=True)

  cards=(await s.execute(select(Apartment.lalafo_id,Apartment.price,Apartment.district,Apartment.phone != "",Apartment.availability_status,func.count(),func.count(func.distinct(CustomerFunnelEvent.telegram_user_id))).join(CustomerFunnelEvent,CustomerFunnelEvent.apartment_id==Apartment.id).where(CustomerFunnelEvent.event_name=="card_opened",CustomerFunnelEvent.created_at>=midnight.astimezone(timezone.utc)).group_by(Apartment.id).order_by(func.count().desc()).limit(20))).all()
  print('CARD_FUNNEL_HEALTH='+json.dumps([{'source_id':a,'price':p,'district':d,'has_contact':contact,'availability':status,'opens':n,'visitors':u} for a,p,d,contact,status,n,u in cards],ensure_ascii=False),flush=True)

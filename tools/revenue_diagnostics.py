"""Read aggregate funnel and confirmed payment history; never expose customers."""
import json
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select,func
from app.models import CustomerFunnelEvent,PaymentRequest,PaymentHistory,Apartment
from app.promotions import publications_in_window


async def _period_summary(session, start, end):
 payments=(await session.execute(select(
  func.count(),func.coalesce(func.sum(PaymentHistory.amount),0),
  func.count(func.distinct(PaymentHistory.telegram_user_id)),
 ).where(PaymentHistory.paid_at>=start,PaymentHistory.paid_at<end))).one()
 events=(await session.execute(select(
  CustomerFunnelEvent.event_name,func.count(),
  func.count(func.distinct(CustomerFunnelEvent.telegram_user_id)),
 ).where(CustomerFunnelEvent.created_at>=start,CustomerFunnelEvent.created_at<end)
  .group_by(CustomerFunnelEvent.event_name))).all()
 requests=(await session.execute(select(PaymentRequest.status,func.count()).where(
  PaymentRequest.created_at>=start,PaymentRequest.created_at<end,
 ).group_by(PaymentRequest.status))).all()
 publications=await publications_in_window(session,start,end)
 return {
  'start':start.isoformat(),'end':end.isoformat(),
  'confirmed_payments':payments[0],'confirmed_revenue_som':payments[1],
  'paying_users':payments[2],
  'events':{name:{'events':n,'users':u} for name,n,u in events},
  'new_requests_current_status':dict(requests),
  'publications':len(publications),
  'publication_sellers':{
   kind:sum(row.seller_type==kind for row in publications)
   for kind in ('owner','realtor','unknown')
  },
 }


async def _revenue_segments(session, start, end):
 seller=func.coalesce(Apartment.seller_type,'unknown')
 seller_rows=(await session.execute(select(
  seller,func.count(),
  func.coalesce(func.sum(PaymentHistory.amount),0),
 ).select_from(PaymentHistory).outerjoin(Apartment,Apartment.id==PaymentHistory.apartment_id)
  .where(PaymentHistory.paid_at>=start,PaymentHistory.paid_at<end)
  .group_by(seller))).all()
 district=func.coalesce(Apartment.district,'unknown')
 district_rows=(await session.execute(select(
  district,func.count(),
  func.coalesce(func.sum(PaymentHistory.amount),0),
 ).select_from(PaymentHistory).outerjoin(Apartment,Apartment.id==PaymentHistory.apartment_id)
  .where(PaymentHistory.paid_at>=start,PaymentHistory.paid_at<end)
  .group_by(district)
  .order_by(func.sum(PaymentHistory.amount).desc()).limit(15))).all()
 return {
  'start':start.isoformat(),'end':end.isoformat(),
  'by_seller':{kind:{'payments':count,'revenue_som':amount}
               for kind,count,amount in seller_rows},
  'top_districts':[{'district':district,'payments':count,'revenue_som':amount}
                   for district,count,amount in district_rows],
 }

async def report(sessions):
 now=datetime.now(timezone.utc);local=now.astimezone(ZoneInfo('Asia/Bishkek'));midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
 async with sessions() as s:
  week_start_local=midnight-timedelta(days=local.weekday())
  current_start=week_start_local.astimezone(timezone.utc)
  prior_start=(week_start_local-timedelta(days=7)).astimezone(timezone.utc)
  elapsed=now-current_start
  prior_same_end=prior_start+elapsed
  prior_full_end=current_start
  current=await _period_summary(s,current_start,now)
  prior_same=await _period_summary(s,prior_start,prior_same_end)
  prior_full=await _period_summary(s,prior_start,prior_full_end)
  print('REVENUE_WEEK_COMPARISON='+json.dumps({
   'current_week':current,'previous_week_same_elapsed':prior_same,
   'previous_week_full':prior_full,
   'revenue_change_same_elapsed_som':current['confirmed_revenue_som']-prior_same['confirmed_revenue_som'],
  },ensure_ascii=False),flush=True)
  print('REVENUE_SEGMENTS_CURRENT='+json.dumps(
   await _revenue_segments(s,current_start,now),ensure_ascii=False),flush=True)
  print('REVENUE_SEGMENTS_PREVIOUS='+json.dumps(
   await _revenue_segments(s,prior_start,prior_same_end),ensure_ascii=False),flush=True)
  for days in range(14):
   day_start=(midnight-timedelta(days=days)).astimezone(timezone.utc)
   day_end=day_start+timedelta(days=1)
   print('REVENUE_DAILY_FULL='+json.dumps(
    await _period_summary(s,day_start,min(day_end,now)),ensure_ascii=False),flush=True)
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

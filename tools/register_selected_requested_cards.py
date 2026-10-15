"""Enroll this explicit batch using actual Telegram receipts, without sending."""
import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, LalafoAutoReplyMeta
from app.promotions import register_requested, record_delivery

async def main(apply):
    ids=[int(value) for value in os.environ['SELECTED_PROMOTION_IDS'].split()]
    if not ids or len(ids)>20 or len(set(ids))!=len(ids):raise ValueError('Invalid requested batch')
    key='requested_batch:'+os.environ['SELECTED_PROMOTION_BATCH_KEY']
    if len(key)>255:raise ValueError('Invalid batch key')
    engine,sessions=create_engine_and_session(get_settings().database_url)
    now=datetime.now(timezone.utc)
    try:
        async with sessions.begin() as session:
            marker=await session.get(LalafoAutoReplyMeta,key)
            if marker:
                print('REQUESTED_BATCH='+marker.value)
                return
            rows=list((await session.scalars(select(Apartment).where(Apartment.lalafo_id.in_(ids)).with_for_update())).all())
            if {a.lalafo_id for a in rows}!=set(ids):raise RuntimeError('Some requested cards are not in the database')
            receipts=[]
            for a in rows:
                sent=a.published_at
                if sent and sent.tzinfo is None:sent=sent.replace(tzinfo=timezone.utc)
                if not a.active or not a.phone or not a.telegram_message_id or not sent or not now-timedelta(hours=2)<=sent<=now:
                    raise RuntimeError(f'Recent verified Telegram receipt is missing for {a.lalafo_id}')
                if apply:
                    await register_requested(session,a,now=sent)
                    await record_delivery(session,a,chat_id=a.telegram_chat_id,message_id=a.telegram_message_id,published_at=sent)
                receipts.append({'source_id':a.lalafo_id,'message_id':a.telegram_message_id,'sent_at':sent.isoformat(),
                    'expires_at':(sent+timedelta(days=1)).isoformat(),'price':a.price,'rooms':a.rooms,'district':a.district,'deposit':a.deposit,
                    'photo_urls':a.photo_urls})
            data=json.dumps({'batch':key,'total_sends':3,'duration_hours':24,'cards':receipts},ensure_ascii=False)
            if apply:session.add(LalafoAutoReplyMeta(key=key,value=data))
            print('REQUESTED_BATCH='+data)
    finally:await engine.dispose()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    asyncio.run(main(parser.parse_args().apply))

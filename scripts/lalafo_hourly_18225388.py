"""Hourly first greeting with a persistent, profile-scoped no-replay ledger."""
from __future__ import annotations
import asyncio,hashlib,json,time,uuid
from pathlib import Path
from urllib.parse import urlencode
import aiohttp,socketio
from sqlalchemy import text
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import LalafoAutoReplyMeta
from app.lalafo.chat_protocol import (LalafoChatClient,LalafoSession,LalafoGatewayError,
    AutoReplyJob,MESSAGE_PAGE_SIZE,is_eligible_incoming,socket_connection_id,
    LALAFO_SOCKET_ORIGIN,LALAFO_SOCKET_PATH)
from tools.lalafo_campaign_session import load_session,storage_cipher

OWNER='18225388'
PREFIX='hourly18225388:'
ENVELOPE='tools/hourly_18225388_encrypted.json'

class StrictClient(LalafoChatClient):
    async def get_session(self):
        if self.session is None:raise LalafoGatewayError('auth')
        return self.session

async def history(client,chat):
    found={};cursor=None
    for _ in range(200):
        page=await client.retrieve_messages(chat,cursor)
        for m in page:found[m.id]=m
        if len(page)<MESSAGE_PAGE_SIZE:return sorted(found.values(),key=lambda m:(m.created_at,m.id))
        oldest=min(page,key=lambda m:(m.created_at,m.id))
        if oldest.id==cursor:raise RuntimeError('HistoryPaginationIncomplete')
        cursor=oldest.id
    raise RuntimeError('HistoryPaginationIncomplete')

def ledger_key(chat_key):return PREFIX+hashlib.sha256(chat_key.encode()).hexdigest()[:40]

def candidate(messages,chat,cutoff):
    return (bool(messages) and messages[0].created_at>=cutoff
        and not any(m.origin_id==OWNER for m in messages)
        and is_eligible_incoming(messages[-1],OWNER,can_send_text=chat.can_send_text,is_blocked=chat.is_blocked))

async def main():
    settings=get_settings();engine,sessions=create_engine_and_session(settings.database_url)
    report={'profile_id':int(OWNER),'status':'starting','sent':0,'verified':0,'reserved_unverified':0}
    client=None;sock=None;http=None;lock=None
    try:
        lock=await engine.connect()
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(1822538801)')):
            report['status']='already_running';return report
        envelope=json.loads(Path(ENVELOPE).read_text())
        seed=await load_session(envelope['sealed_session'],key_name=envelope['key_name'])
        if str(seed['profile_id'])!=OWNER or str(seed['login']['id'])!=OWNER:raise RuntimeError('WrongProfile')
        cipher=storage_cipher(settings.require_callback_secret())
        async with sessions() as s:
            row=await s.get(LalafoAutoReplyMeta,PREFIX+'session')
            login=json.loads(cipher.decrypt(row.value.encode())) if row else seed['login']
        client=StrictClient(login='',password='',fingerprint='hourly-18225388',timeout=25)
        def install(p):
            if str(p.get('id'))!=OWNER:raise RuntimeError('WrongProfile')
            client._session=LalafoSession(OWNER,p['token'],p['access_token'],p['user_hash'])
        install(login)
        try:chats=await client.list_chats()
        except LalafoGatewayError as exc:
            if exc.kind!='auth':raise
            r=await client._http.post('/api/auth/renew-session',json={'refresh_token':login['refresh_token']},headers=client._request_headers(authenticated=False,mutation=True))
            if r.status_code!=200:raise LalafoGatewayError('auth',status=r.status_code)
            renewed=r.json();install(renewed)
            login=renewed
            async with sessions.begin() as s:
                value=cipher.encrypt(json.dumps(login).encode()).decode()
                row=await s.get(LalafoAutoReplyMeta,PREFIX+'session')
                if row:row.value=value
                else:s.add(LalafoAutoReplyMeta(key=PREFIX+'session',value=value))
            chats=await client.list_chats()
        report['chats_scanned']=len(chats)
        baseline=set(seed['baseline']);cutoff=seed['activated_at'];pending=[]
        for chat in chats:
            if chat.chat_key in baseline:continue
            key=ledger_key(chat.chat_key)
            async with sessions() as s:row=await s.get(LalafoAutoReplyMeta,key)
            if row:
                saved=json.loads(row.value)
                if saved.get('status') in ('reserved','sent_unverified'):
                    messages=await history(client,chat)
                    if any(m.origin_id==OWNER for m in messages):
                        async with sessions.begin() as s:
                            r=await s.get(LalafoAutoReplyMeta,key);r.value=json.dumps({**saved,'status':'verified'})
                        report['verified']+=1
                    else:report['reserved_unverified']+=1
                continue
            messages=await history(client,chat)
            if candidate(messages,chat,cutoff):pending.append((chat,messages[-1]))
        report['candidates']=len(pending)
        if pending:
            http=aiohttp.ClientSession(trust_env=True)
            sock=socketio.AsyncClient(http_session=http,reconnection=False,logger=False,engineio_logger=False)
            ready=asyncio.Event()
            async def accept(payload):
                sid=socket_connection_id(payload)
                if sid:client.set_socket_id(sid);ready.set()
            @sock.on('message')
            async def on_message(data,*args):await accept(data)
            @sock.on('SocketConnection')
            async def on_connection(data,*args):await accept({**(data if isinstance(data,dict) else {}),'ref':'SocketConnection'})
            await sock.connect(LALAFO_SOCKET_ORIGIN+'?'+urlencode({'token':client.session.access_token}),socketio_path=LALAFO_SOCKET_PATH,transports=['polling'],wait_timeout=20)
            await asyncio.wait_for(ready.wait(),20)
        for chat,inbound in pending:
            # Re-read immediately before reserving, in case a human replied.
            messages=await history(client,chat)
            if not candidate(messages,chat,cutoff):continue
            key=ledger_key(chat.chat_key);now=int(time.time()*1000);ack=str(uuid.uuid4())
            saved={'status':'reserved','ack':ack,'inbound_id':inbound.id,'at':now}
            async with sessions.begin() as s:
                if await s.get(LalafoAutoReplyMeta,key):continue
                s.add(LalafoAutoReplyMeta(key=key,value=json.dumps(saved)))
            job=AutoReplyJob(inbound_key=key,chat_key=chat.chat_key,inbound_id=inbound.id,inbound_time=inbound.created_at,ack=ack,source='hourly',status='sending',attempts=1,next_attempt_at=now,needs_reconcile=False,first_attempt_at=now,created_at=now,updated_at=now)
            try:
                await client.send_reply(job);report['sent']+=1
                saved['status']='sent_unverified'
                await asyncio.sleep(.8)
                if await client.reconcile_reply(job):saved['status']='verified';report['verified']+=1
                else:report['reserved_unverified']+=1
            except LalafoGatewayError as exc:
                # Reservation is permanent until history verifies delivery.
                if exc.kind in ('security_challenge','auth','blocked'):raise
                if await client.reconcile_reply(job):saved['status']='verified';report['verified']+=1
                else:report['reserved_unverified']+=1
            finally:
                async with sessions.begin() as s:
                    row=await s.get(LalafoAutoReplyMeta,key);row.value=json.dumps(saved)
            await asyncio.sleep(.7)
        report['status']='partial' if report['reserved_unverified'] else 'ok'
        return report
    except LalafoGatewayError as exc:
        report.update(status='failed',error=exc.kind,http_status=exc.status);return report
    except Exception as exc:
        report.update(status='failed',error=type(exc).__name__);return report
    finally:
        if sock and sock.connected:await sock.disconnect()
        if http:await http.close()
        if client:await client.close()
        if lock:await lock.close()
        await engine.dispose()
        print('HOURLY_18225388_RESULT '+json.dumps(report),flush=True)

if __name__=='__main__':
    result=asyncio.run(main())
    raise SystemExit(0 if result['status'] in ('ok','already_running') else 1)

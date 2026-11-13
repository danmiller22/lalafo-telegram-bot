"""Apply contact exclusions and refine recent public cards without new posts."""
import asyncio,json
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,update
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest,TelegramRetryAfter
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment,ApartmentInventoryQueue
from app.lalafo.exclusions import is_excluded_contact
from app.location import public_location
from app.telegram.formatting import format_public_apartment,is_confirmed_owner
from app.telegram.keyboards import apartment_keyboard
from app.security import TokenSigner

async def main():
    settings=get_settings();engine,sessions=create_engine_and_session(settings.database_url)
    bot=Bot(settings.require_bot_token());stats={'queued_excluded':0,'locations_updated':0,'unchanged':0,'edit_errors':0}
    try:
        async with sessions.begin() as s:
            rows=list((await s.scalars(select(Apartment).join(ApartmentInventoryQueue).where(ApartmentInventoryQueue.status=='queued'))).all())
            blocked=[a.id for a in rows if is_excluded_contact(a)]
            if blocked:
                result=await s.execute(update(ApartmentInventoryQueue).where(ApartmentInventoryQueue.apartment_id.in_(blocked),ApartmentInventoryQueue.status=='queued').values(status='skipped',last_error='excluded_contact'))
                stats['queued_excluded']=result.rowcount
            recent=list((await s.scalars(select(Apartment).where(
                Apartment.telegram_chat_id==settings.telegram_group_id,
                Apartment.telegram_message_id.is_not(None),
                Apartment.published_at>=datetime.now(timezone.utc)-timedelta(hours=48),
            ).order_by(Apartment.published_at.desc()).limit(150))).all())
        signer=TokenSigner(settings.require_callback_secret())
        for a in recent:
            if is_excluded_contact(a) or public_location(a)==a.district:continue
            archived=not a.active or a.availability_status=='unavailable'
            kwargs={}
            if not archived:
                kwargs['reply_markup']=apartment_keyboard(a.id,phone=a.phone,confirmed_owner=is_confirmed_owner(a),signer=signer,bot_username=settings.telegram_bot_username,support_url=settings.support_url)
            for attempt in range(2):
                try:
                    await bot.edit_message_text(chat_id=a.telegram_chat_id,message_id=a.telegram_message_id,
                        text=format_public_apartment(a,bot_username=settings.telegram_bot_username,wanted_deep_link=True,archived=archived),parse_mode='HTML',**kwargs)
                    stats['locations_updated']+=1;break
                except TelegramRetryAfter as exc:
                    if attempt:stats['edit_errors']+=1;break
                    await asyncio.sleep(exc.retry_after+1)
                except TelegramBadRequest as exc:
                    if 'message is not modified' in str(exc).lower():stats['unchanged']+=1
                    else:stats['edit_errors']+=1
                    break
            await asyncio.sleep(1)
        print('PRECISE_LOCATION_POLICY '+json.dumps(stats),flush=True)
    finally:
        await bot.session.close();await engine.dispose()
if __name__=='__main__':asyncio.run(main())

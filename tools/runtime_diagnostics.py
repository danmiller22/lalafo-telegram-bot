"""Read-only private lookup of publication receipts for a supplied phone hash."""
import asyncio, hashlib, json, re
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, RequestedCardPublication, DailyFeaturedPublication
TARGET = "7793dafa4b4bce33afe445566fc56d1a98205974703180e58f41173bc6b7a582"
CHAT = -1004389602150
async def main():
 engine, sessions = create_engine_and_session(get_settings().database_url)
 try:
  async with sessions() as s:
   # Read candidate contacts without emitting them, then compare normalized suffix hashes.
   rows = (await s.execute(select(Apartment.id, Apartment.phone, Apartment.rooms, Apartment.district, Apartment.price, Apartment.telegram_chat_id, Apartment.telegram_message_id, Apartment.publication_status, Apartment.availability_status, Apartment.active))).all()
   matches = [row for row in rows if hashlib.sha256(re.sub(r"\D", "", row.phone or "")[-9:].encode()).hexdigest() == TARGET]
   out=[]
   for row in matches:
    receipts = (await s.execute(select(RequestedCardPublication.telegram_message_id).where(RequestedCardPublication.apartment_id == row.id, RequestedCardPublication.telegram_chat_id == CHAT))).scalars().all()
    receipts += (await s.execute(select(DailyFeaturedPublication.telegram_message_id).where(DailyFeaturedPublication.source_apartment_id == row.id, DailyFeaturedPublication.telegram_chat_id == CHAT, DailyFeaturedPublication.telegram_message_id.is_not(None)))).scalars().all()
    if row.telegram_chat_id == CHAT and row.telegram_message_id: receipts.append(row.telegram_message_id)
    out.append({"rooms": row.rooms, "district": row.district, "price": row.price, "publication_status": row.publication_status, "availability_status": row.availability_status, "active": row.active, "message_ids": sorted(set(receipts))})
   print("PHONE_LOOKUP=" + json.dumps(out, ensure_ascii=False))
 finally: await engine.dispose()
if __name__ == "__main__": asyncio.run(main())

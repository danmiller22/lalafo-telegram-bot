"""Enroll the last five explicitly requested cards using public Telegram receipts."""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from app.config import get_settings
from app.database import create_engine_and_session
from app.models import Apartment, RequestedCardPromotion
from app.promotions import record_delivery, register_requested, promotion_work
from app.inventory import publication_day_start
from datetime import timedelta

async def run(apply=False):
    manifest = json.loads(Path('tools/requested_card_promotions_20261014.json').read_text())
    engine, sessions = create_engine_and_session(get_settings().database_url)
    now = datetime.now(timezone.utc)
    try:
        async with sessions.begin() as session:
            for card in manifest['cards']:
                apartment = await session.scalar(select(Apartment).where(Apartment.lalafo_id == card['source_id']))
                if apartment is None or not apartment.active or not apartment.phone:
                    raise RuntimeError(f"Requested apartment {card['source_id']} is missing or unavailable")
                receipts = [(r['message_id'], datetime.fromisoformat(r['published_at'].replace('Z', '+00:00'))) for r in card['receipts']]
                latest = max(date for _, date in receipts)
                promotion = await session.get(RequestedCardPromotion, apartment.id)
                # Rerunning registration cannot restart the 24-hour window.
                if promotion is None or promotion.requested_at.replace(tzinfo=timezone.utc) < latest:
                    await register_requested(session, apartment, now=latest)
                for message_id, sent_at in receipts:
                    await record_delivery(session, apartment, chat_id=manifest['chat_id'], message_id=message_id, published_at=sent_at)
                print(json.dumps({'source_id':card['source_id'], 'requested_at':latest.isoformat(), 'expires_at':(latest+timedelta(days=1)).isoformat(), 'total_limit':3, 'imported_receipts':len(receipts)}))
            start = publication_day_start(now).astimezone(timezone.utc)
            due, reserved = await promotion_work(session, now=now, day_start=start, day_end=start+timedelta(hours=21))
            print(json.dumps({'apply':apply, 'registered':len(manifest['cards']), 'due_now':len(due), 'remaining_slots_today':reserved}))
            if not apply:
                await session.rollback()
    finally:
        await engine.dispose()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    asyncio.run(run(parser.parse_args().apply))

"""Restore the standard keyboard on recent managed-card Telegram receipts."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select

from app.config import get_settings
from app.database import create_engine_and_session
from app.managed_cards import managed_bindings
from app.models import Apartment, RequestedCardPublication, DailyFeaturedPublication
from app.security import TokenSigner
from app.telegram.keyboards import apartment_keyboard


def followed_run_complete(run_id):
    request = Request(
        f'https://api.github.com/repos/danmiller22/lalafo-telegram-bot/actions/runs/{run_id}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json'},
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)['status'] == 'completed'


async def run():
    settings = get_settings()
    engine, sessions = create_engine_and_session(settings.database_url)
    bot = Bot(token=settings.require_bot_token())
    signer = TokenSigner(settings.require_callback_secret())
    completed = set()
    failed = set()
    removed = set()
    follow = os.environ.get('FOLLOW_PUBLICATION_RUN', '')
    if follow and not follow.isdigit():
        raise ValueError('Invalid publication run ID')
    deadline = datetime.now(timezone.utc) + timedelta(minutes=65)
    try:
        while True:
            # Check completion first, then read receipts so the final send cannot
            # race the last repair pass.
            done = not follow or await asyncio.to_thread(followed_run_complete, follow)
            async with sessions() as session:
                ids = list(await managed_bindings(session))
                all_cards = os.environ.get('ALL_PUBLIC_CARDS') == 'true'
                phones = dict((await session.execute(select(Apartment.id, Apartment.phone))).all())
                cutoff = datetime.now(timezone.utc) - timedelta(days=1)
                rows = set((await session.execute(select(
                    RequestedCardPublication.apartment_id,
                    RequestedCardPublication.telegram_chat_id,
                    RequestedCardPublication.telegram_message_id,
                ).where(True if all_cards else RequestedCardPublication.apartment_id.in_(ids),
                        True if all_cards else RequestedCardPublication.published_at >= cutoff))).all())
                rows.update((await session.execute(select(
                    Apartment.id, Apartment.telegram_chat_id, Apartment.telegram_message_id,
                ).where(True if all_cards else Apartment.id.in_(ids)))).all())
                if all_cards:
                    rows.update((await session.execute(select(
                        DailyFeaturedPublication.source_apartment_id,
                        DailyFeaturedPublication.telegram_chat_id,
                        DailyFeaturedPublication.telegram_message_id,
                    ).where(DailyFeaturedPublication.telegram_message_id.is_not(None)))).all())
            rows = {row for row in rows if row[0] is not None and row[1] is not None and row[2] is not None}
            for apartment_id, chat_id, message_id in sorted(rows):
                key = (chat_id, message_id)
                if chat_id != settings.telegram_group_id or not message_id or key in completed:
                    continue
                markup = apartment_keyboard(apartment_id, phone=phones.get(apartment_id), signer=signer,
                    bot_username=settings.telegram_bot_username, support_url=settings.support_bot_url)
                try:
                    while True:
                        try:
                            await bot.edit_message_reply_markup(chat_id=chat_id,
                                message_id=message_id, reply_markup=markup)
                            break
                        except TelegramRetryAfter as exc:
                            await asyncio.sleep(exc.retry_after + 1)
                except TelegramBadRequest as exc:
                    if 'message to edit not found' in str(exc).lower():
                        removed.add(key)
                        continue
                    if 'message is not modified' not in str(exc).lower():
                        failed.add(key)
                        print(f'BUTTON_REPAIR_FAILED apartment_id={apartment_id} message_id={message_id}', flush=True)
                        continue
                completed.add(key)
                failed.discard(key)
                print(f'BUTTONS_RESTORED apartment_id={apartment_id} message_id={message_id}', flush=True)
                await asyncio.sleep(0.05)
            if done:
                break
            if datetime.now(timezone.utc) >= deadline:
                raise RuntimeError('Followed publication run still active; repair deadline reached')
            await asyncio.sleep(30)
        print(f'BUTTON_REPAIR_COMPLETE restored={len(completed)} removed={len(removed)} failed={len(failed)}', flush=True)
        return 1 if failed else 0
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))

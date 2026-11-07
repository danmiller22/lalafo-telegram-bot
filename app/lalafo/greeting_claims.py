"""Persistent reservations prevent a second greeting after an uncertain send."""
from __future__ import annotations

import hashlib
import json
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import LalafoAutoReplyMeta


class GreetingClaims:
    def __init__(self, sessions):
        self.sessions = sessions
        self.dialect = sessions.kw['bind'].dialect.name

    @staticmethod
    def key(owner_id: str, chat_key: str) -> str:
        digest = hashlib.sha256(f'{owner_id}:{chat_key}'.encode()).hexdigest()[:48]
        return f'greeting_once:{digest}'

    async def claim(self, owner_id: str, chat_key: str, ack: str) -> bool:
        key = self.key(owner_id, chat_key)
        value = json.dumps({'status': 'sending', 'ack': ack})
        insert = {'postgresql': pg_insert, 'sqlite': sqlite_insert}[self.dialect]
        statement = insert(LalafoAutoReplyMeta).values(key=key, value=value)
        statement = statement.on_conflict_do_nothing(index_elements=['key']).returning(LalafoAutoReplyMeta.key)
        async with self.sessions.begin() as session:
            return (await session.scalar(statement)) is not None

    async def finish(self, owner_id: str, chat_key: str, status: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(LalafoAutoReplyMeta, self.key(owner_id, chat_key))
            data = json.loads(row.value)
            data['status'] = status
            row.value = json.dumps(data)

"""Independent, durable admin-only Lalafo publisher. No payment bot runtime.

Run: uvicorn app.manual_service:app --host 0.0.0.0 --port $PORT
Only LALAFO_BOT_TOKEN is used for Telegram; no polling or deleteWebhook calls.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
import os
import secrets
from urllib.parse import urlsplit

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import BigInteger, Column, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text as sql_text

from app.bot.lalafo_links import extract_lalafo_url, normalize_district
from app.config import Settings, get_settings
from app.database import create_engine_and_session
from app.lalafo.client import LalafoAccessError, LalafoClient, LalafoError, LalafoNotFound
from app.lalafo.parser import LalafoParseError
from app.payments.repository import ApartmentRepository
from app.security import TokenSigner
from app.telegram.publisher import TelegramPublisher
from scripts.publish_inventory import _valid

logger = logging.getLogger(__name__)
PERSONAL_BOT_DISABLED = True
metadata = MetaData()
jobs = Table(
    "manual_lalafo_updates", metadata,
    Column("update_id", BigInteger, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("status", String(20), nullable=False),
)
conversations = Table(
    "manual_lalafo_conversations", metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("source_url", Text, nullable=False),
)


def validate_config(settings: Settings, webhook_url: str, webhook_secret: str) -> str:
    token = settings.require_lalafo_bot_token()
    if token == settings.telegram_bot_token or token.split(":", 1)[0] == "8867149259":
        raise RuntimeError("Payment bot token is forbidden in the manual service")
    if settings.admin_user_id <= 0:
        raise RuntimeError("ADMIN_USER_ID is required; username authorization is disabled")
    parts = urlsplit(webhook_url)
    if parts.scheme != "https" or not parts.hostname or parts.path != "/telegram/manual-webhook":
        raise RuntimeError("LALAFO_WEBHOOK_URL must end in /telegram/manual-webhook")
    if len(webhook_secret) < 32:
        raise RuntimeError("LALAFO_WEBHOOK_SECRET must contain at least 32 characters")
    settings.require_callback_secret()
    return token


class ManualPublisher:
    def __init__(self, settings: Settings, bot: Bot, engine, sessions):
        self.settings, self.bot, self.engine = settings, bot, engine
        self.apartments = ApartmentRepository(sessions)
        self.signer = TokenSigner(settings.require_callback_secret())
        self.wake = asyncio.Event()
        self.last_error: str | None = None

    async def enqueue(self, payload: dict) -> bool:
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(jobs).values(
                    update_id=payload["update_id"], payload=json.dumps(payload), status="pending"
                ))
        except IntegrityError:
            return False  # Telegram retry: never publish the same update twice.
        self.wake.set()
        return True

    async def source(self, chat_id: int) -> str | None:
        async with self.engine.connect() as conn:
            return await conn.scalar(select(conversations.c.source_url).where(
                conversations.c.chat_id == chat_id
            ))

    async def remember(self, chat_id: int, url: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(conversations.delete().where(conversations.c.chat_id == chat_id))
            if url:
                await conn.execute(insert(conversations).values(chat_id=chat_id, source_url=url))

    async def handle(self, payload: dict) -> None:
        message = payload["message"]
        chat_id, text = message["chat"]["id"], message.get("text", "").strip()
        if text.split(" ", 1)[0].lower() in {"/start", "/cancel", "отмена", "cancel"}:
            await self.remember(chat_id, "")
            await self.bot.send_message(chat_id, "Пришлите ссылку Lalafo. Затем я спрошу район и опубликую квартиру в группе.")
            return
        url = extract_lalafo_url(text)
        if url:
            await self.remember(chat_id, url)
            await self.bot.send_message(chat_id, "Какой район написать в заголовке карточки?")
            return
        url = await self.source(chat_id)
        if not url:
            await self.bot.send_message(chat_id, "Сначала пришлите ссылку на объявление Lalafo.")
            return
        district = normalize_district(text)
        if not district or text.startswith("/"):
            await self.bot.send_message(chat_id, "Напишите район текстом — не больше 60 символов. Отмена: /cancel")
            return
        await self.bot.send_message(chat_id, "⏳ Загружаю квартиру…")
        try:
            # No public proxy scans. Bounded requests cannot exhaust payment resources.
            async with asyncio.timeout(60):
                async with LalafoClient(timeout=12, max_retries=1,
                                        proxy_url=self.settings.lalafo_proxy_url) as client:
                    ad = await client.detail(url)
        except LalafoNotFound:
            await self.bot.send_message(chat_id, "Объявление удалено или недоступно. Пришлите другую ссылку.")
            return
        except LalafoAccessError:
            await self.bot.send_message(chat_id, "Lalafo временно запретил загрузку (403/429). Ссылка сохранена. Повторите район позже — ничего не опубликовано.")
            return
        except (LalafoError, LalafoParseError, ValueError, TimeoutError):
            await self.bot.send_message(chat_id, "Не удалось загрузить квартиру. Ссылка сохранена: повторите район позже или пришлите другую ссылку.")
            return
        ad = ad.model_copy(update={"district": district})
        valid, reason = _valid(ad, self.settings)
        if not valid:
            await self.bot.send_message(chat_id, f"Квартира не прошла фильтр: {reason}.")
            return
        apartment = await self.apartments.upsert_discovered(ad, discovery_priority=True)
        publisher = TelegramPublisher(
            self.bot, chat_id=self.settings.telegram_group_id, signer=self.signer,
            bot_username=self.settings.telegram_bot_username,
            support_url=self.settings.support_bot_url,
            max_photos=self.settings.max_photos_per_apartment,
        )
        # Clear the source before sending: ambiguous delivery must never cause an
        # automatic replay. A new link is an explicit new publication request.
        await self.remember(chat_id, "")
        try:
            published = await publisher.publish(apartment.id, ad)
        except Exception:
            self.last_error = "publication_delivery_uncertain"
            await self.bot.send_message(chat_id, "Telegram не подтвердил полную публикацию. Проверьте группу перед повторной отправкой ссылки — часть карточки могла появиться.")
            return
        try:
            await self.apartments.mark_published(apartment.id,
                chat_id=self.settings.telegram_group_id, message_id=published.message_id)
        except Exception:
            self.last_error = "publication_metadata_failed"
            await self.bot.send_message(chat_id, "Карточка опубликована, но отметка в базе не сохранена. Повторно не отправляйте ссылку.")
            return
        await self.bot.send_message(chat_id, f"✅ Квартира опубликована. ID: {ad.lalafo_id}.")

    async def work(self) -> None:
        # Hold a session lock across rolling deployments so only one worker
        # consumes the queue and chat state, even while two instances coexist.
        async with self.engine.connect() as lock:
            if self.engine.dialect.name == "postgresql":
                while not await lock.scalar(sql_text("SELECT pg_try_advisory_lock(8911032573)")):
                    await asyncio.sleep(2)
            try:
                await self._work_locked()
            finally:
                if self.engine.dialect.name == "postgresql":
                    await lock.execute(sql_text("SELECT pg_advisory_unlock(8911032573)"))

    async def _work_locked(self) -> None:
        # A killed process might have sent an album before saving completion.
        # Retain these records for inspection; never automatically replay them.
        async with self.engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.status == "processing").values(status="uncertain"))
        while True:
            self.wake.clear()
            async with self.engine.begin() as conn:
                row = (await conn.execute(select(jobs).where(jobs.c.status == "pending")
                                          .order_by(jobs.c.update_id).limit(1))).mappings().first()
                if row:
                    await conn.execute(update(jobs).where(jobs.c.update_id == row["update_id"])
                                       .values(status="processing"))
            if row is None:
                try:
                    await asyncio.wait_for(self.wake.wait(), 5)
                except TimeoutError:
                    pass
                continue
            outcome = "done"
            try:
                async with asyncio.timeout(180):
                    await self.handle(json.loads(row["payload"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcome = "uncertain"
                self.last_error = type(exc).__name__
                logger.error("Manual update failed: %s", type(exc).__name__)
            async with self.engine.begin() as conn:
                await conn.execute(update(jobs).where(jobs.c.update_id == row["update_id"])
                                   .values(status=outcome))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    url = os.environ.get("LALAFO_WEBHOOK_URL", "")
    secret = os.environ.get("LALAFO_WEBHOOK_SECRET", "")
    token = validate_config(settings, url, secret)
    bot = Bot(token=token)
    engine, sessions = create_engine_and_session(settings.database_url)
    worker = None
    try:
        me = await bot.get_me()
        if me.username != "personn22bot":
            raise RuntimeError("Expected @personn22bot; refusing to change another bot webhook")
        if PERSONAL_BOT_DISABLED:
            # The owner workflow now lives exclusively in the main payment bot.
            # Remove Telegram delivery from the retired personal bot before
            # keeping the Render health endpoint alive for observability.
            await bot.delete_webhook(drop_pending_updates=True)
            app.state.disabled = True
            app.state.runtime = None
            app.state.worker = None
            app.state.secret = ""
            yield
            return
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        runtime = ManualPublisher(settings, bot, engine, sessions)
        app.state.runtime = runtime
        app.state.secret = secret
        worker = asyncio.create_task(runtime.work(), name="manual-lalafo-worker")
        app.state.worker = worker
        await bot.set_webhook(url, secret_token=secret, allowed_updates=["message"],
                              max_connections=1, drop_pending_updates=False)
        await bot.set_my_name(name="Arenda.KG")
        yield
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()
        await engine.dispose()


app = FastAPI(title="Arenda.KG manual Lalafo publisher", lifespan=lifespan)


@app.get("/health")
async def health():
    if getattr(app.state, "disabled", False):
        return {
            "status": "ok",
            "bot": "personn22bot",
            "worker": "disabled",
            "last_error": None,
        }
    runtime = getattr(app.state, "runtime", None)
    worker = getattr(app.state, "worker", None)
    ready = runtime is not None and worker is not None and not worker.done()
    return JSONResponse(status_code=200 if ready else 503, content={
        "status": "ok" if ready else "error", "bot": "personn22bot",
        "worker": "running" if ready else "stopped",
        "last_error": runtime.last_error if runtime else None,
    })


@app.post("/telegram/manual-webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if getattr(app.state, "disabled", False):
        raise HTTPException(410, "Personal bot is disabled; use the main bot")
    expected = getattr(app.state, "secret", "")
    if not expected or not secrets.compare_digest(expected, x_telegram_bot_api_secret_token):
        raise HTTPException(401)
    runtime = app.state.runtime
    if app.state.worker.done():
        raise HTTPException(503)
    payload = await request.json()
    message = payload.get("message", {})
    if (message.get("chat", {}).get("type") != "private"
        or message.get("from", {}).get("id") != runtime.settings.admin_user_id
        or not isinstance(message.get("text"), str)):
        return {"ok": True}
    if not isinstance(payload.get("update_id"), int):
        raise HTTPException(400)
    await runtime.enqueue(payload)
    return {"ok": True}


@app.post("/diagnostics/lalafo", include_in_schema=False)
async def diagnose_lalafo(request: Request, x_lalafo_diagnostic_secret: str = Header(default="")):
    """Read-only cloud egress check. Never enqueue, save, or publish an ad."""
    expected = getattr(app.state, "secret", "")
    if not expected or not secrets.compare_digest(expected, x_lalafo_diagnostic_secret):
        raise HTTPException(401)
    payload = await request.json()
    url = extract_lalafo_url(payload.get("url"))
    if not url:
        raise HTTPException(400, "Expected a Lalafo advertisement URL")
    try:
        async with asyncio.timeout(60):
            async with LalafoClient(timeout=12, max_retries=1,
                                    proxy_url=app.state.runtime.settings.lalafo_proxy_url) as client:
                ad = await client.detail(url)
    except (LalafoError, LalafoParseError, ValueError, TimeoutError) as exc:
        return JSONResponse(status_code=503, content={"ok": False, "error": type(exc).__name__})
    return {"ok": True, "lalafo_id": ad.lalafo_id,
            "photo_count": len(ad.photo_urls), "has_phone": bool(ad.phone)}

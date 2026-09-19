# Independent manual Lalafo publisher

The payment service remains `app.web:app`. The manual publisher runs in a
separate cloud web service: `uvicorn app.manual_service:app --host 0.0.0.0 --port $PORT`.
Do not run the Windows worker or `app.bot.lalafo_only` alongside it.

Required environment:
- LALAFO_BOT_TOKEN: token belonging to @personn22bot ONLY.
- ADMIN_USER_ID: numeric Telegram administrator ID.
- DATABASE_URL: existing apartment database (PostgreSQL).
- CALLBACK_SECRET: same signing secret as the payment service.
- LALAFO_WEBHOOK_URL: https://<manual-service>/telegram/manual-webhook
- LALAFO_WEBHOOK_SECRET: independent random secret, at least 32 characters.
- TELEGRAM_GROUP_ID: -1004389602150
- TELEGRAM_BOT_USERNAME: arenda312bot (contact/payment links only).

Do NOT supply TELEGRAM_BOT_TOKEN. The service validates bot identity before
setting its webhook. It never deletes a webhook, starts payment handlers,
changes payment records, or calls the main service's publication relay.
Keep LALAFO_BOT_CLOUD_ENABLED=false in the payment service.
The publishing bot's display name is set to Arenda.KG. It must be a member of
the destination group with permission to send media/messages.

Workflow: administrator sends a URL, bot asks for district, district triggers
loading and publication. /start and /cancel reset the pending link.
Only the configured numeric administrator can enqueue jobs.

The webhook commits updates into a separate durable table before returning
200. Telegram redelivery is deduplicated by update ID. Conversation state
survives process restarts. A PostgreSQL advisory lock serializes workers during
rolling deployments. Interrupted/ambiguous sends are marked uncertain and
never automatically replayed. Inspect these records before resending a link.

Lalafo loading is bounded to 60 seconds; no public proxy discovery is used.
403/429 returns an explanatory response and retains the link for a later retry.
An optional LALAFO_PROXY_URL can supply an operator-managed route. There is no
guarantee that a cloud address can fetch every Lalafo listing. Verify a real
listing by a read-only fetch before claiming end-to-end success.

Verification without Telegram messages: GET /health, Telegram getMe,
getWebhookInfo, getChatMember, and read-only Lalafo detail loading.
Do not send a test card or test message without the user's confirmation.

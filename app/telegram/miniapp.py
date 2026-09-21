from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl

from app.payment_plans import MONTH_PRICE, WEEK_PRICE


@dataclass(frozen=True, slots=True)
class TelegramMiniAppUser:
    id: int
    first_name: str
    username: str | None = None


def verify_telegram_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = 86_400,
    now: int | None = None,
) -> TelegramMiniAppUser | None:
    """Validate Telegram Mini App initData and return its authenticated user."""
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        current_time = int(time.time()) if now is None else now
        if auth_date > current_time + 30 or current_time - auth_date > max_age_seconds:
            return None
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(fields.items())
        )
        secret_key = hmac.new(
            b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied_hash, expected_hash):
            return None
        raw_user = json.loads(fields["user"])
        user_id = int(raw_user["id"])
        first_name = str(raw_user.get("first_name") or "Пользователь")[:255]
        username_value = raw_user.get("username")
        username = str(username_value)[:64] if username_value else None
        return TelegramMiniAppUser(user_id, first_name, username)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def mini_app_html(*, title: str = "Доступ к квартире") -> str:
    safe_title = escape(title)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
  <title>{safe_title}</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--tg-theme-bg-color, #f4f6f7); color: var(--tg-theme-text-color, #15201d); }}
    main {{ max-width: 540px; margin: 0 auto; padding: 16px 14px 28px; }}
    .card {{ background: var(--tg-theme-secondary-bg-color, #fff); border-radius: 20px; padding: 16px; box-shadow: 0 8px 28px #00000012; }}
    h1 {{ font-size: 21px; margin: 0 0 8px; }}
    .plans {{ white-space: pre-line; line-height: 1.6; margin: 14px 0 8px; }}
    .status {{ border-radius: 13px; padding: 12px; margin: 12px 0; background: #12856a18; line-height: 1.4; }}
    .phone {{ font-size: 22px; font-weight: 800; color: #079b79; word-break: break-word; }}
    .photos {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 0 0 14px; }}
    .photos img {{ width: 100%; height: 118px; object-fit: cover; border-radius: 11px; }}
    .details {{ white-space: pre-line; font-size: 16px; font-weight: 650; line-height: 1.55; margin: 4px 0 12px; }}
    button, .button {{ width: 100%; border: 0; border-radius: 14px; padding: 14px 16px; margin-top: 9px; font: inherit; font-weight: 750; text-align: center; cursor: pointer; text-decoration: none; display: block; }}
    .primary {{ background: var(--tg-theme-button-color, #079b79); color: var(--tg-theme-button-text-color, white); }}
    .secondary {{ background: #12856a18; color: var(--tg-theme-link-color, #07866b); }}
    button:disabled {{ cursor: default; opacity: .82; }}
    .hidden {{ display: none !important; }}
    .foot {{ text-align: center; color: var(--tg-theme-hint-color, #6c7a76); font-size: 12px; margin-top: 14px; }}
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1 id="title">Получить доступ</h1>
    <div id="plans" class="plans">1 неделя доступа к номерам — {WEEK_PRICE} сом
1 месяц доступа к номерам — {MONTH_PRICE} сом</div>
    <div id="apartment" class="hidden">
      <div id="photos" class="photos"></div>
      <div id="details" class="details"></div>
    </div>
    <div id="status" class="status">Проверяем доступ…</div>
    <a id="phone" class="phone hidden"></a>
    <button id="pay-week" class="primary hidden">Оплатить неделю — {WEEK_PRICE} сом</button>
    <button id="pay-month" class="primary hidden">Оплатить месяц — {MONTH_PRICE} сом</button>
    <button id="check" class="secondary hidden">Я оплатил(а)</button>
    <button id="checking" class="secondary hidden" disabled>⏳ Статус: оплата проверяется</button>
    <button id="refresh" class="secondary hidden">Обновить статус</button>
  </section>
  <div id="foot" class="foot">Номер виден только пользователю с подтверждённым доступом</div>
</main>
<script>
(() => {{
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {{ tg.ready(); tg.expand(); }}
  const initData = tg ? tg.initData : "";
  const query = new URLSearchParams(location.search);
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || query.get("tgWebAppStartParam") || "";
  const el = id => document.getElementById(id);
  let lastState = "";
  let paymentOpening = false;

  function show(id, visible) {{ el(id).classList.toggle("hidden", !visible); }}
  function message(text) {{ el("status").textContent = text; }}
  async function api(path, body) {{
    const response = await fetch(path, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{init_data: initData, start_param: startParam, ...body}})
    }});
    const data = await response.json().catch(() => ({{detail: "Ошибка сервиса"}}));
    if (!response.ok) throw new Error(data.detail || "Ошибка сервиса");
    return data;
  }}
  function render(data) {{
    lastState = data.status;
    const approved = data.status === "approved";
    el("title").textContent = approved ? "Квартира" : "Получить доступ";
    show("plans", !approved);
    show("apartment", approved);
    show("foot", !approved);
    show("status", !approved);
    show("phone", approved);
    const canPay = data.status !== "approved" && data.status !== "pending";
    show("pay-week", canPay);
    show("pay-month", canPay && Boolean(data.monthly_available));
    show("refresh", !approved && data.status !== "unpaid");
    show("check", data.status === "awaiting_receipt");
    show("checking", data.status === "pending");
    if (approved) {{
      const apartment = data.apartment || {{}};
      const photos = el("photos");
      photos.replaceChildren();
      (apartment.photo_urls || []).forEach(url => {{
        const image = document.createElement("img");
        image.src = url;
        image.alt = "Фото квартиры";
        image.loading = "eager";
        photos.appendChild(image);
      }});
      show("photos", photos.childElementCount > 0);
      const price = Number(apartment.price || 0).toLocaleString("ru-RU");
      const deposit = apartment.deposit ? "\\n🔐 Депозит: " + Number(apartment.deposit).toLocaleString("ru-RU") + " сом" : "";
      el("details").textContent = "🏠 " + (apartment.rooms || "—") + "-комнатная квартира\\n📍 " + (apartment.district || "—") + "\\n🏙 " + (apartment.city || "Бишкек") + "\\n💰 " + price + " сом" + deposit;
      el("phone").textContent = "📞 " + data.phone;
      el("phone").href = "tel:" + String(data.phone || "").replace(/\\s+/g, "");
    }} else if (data.status === "pending") {{
      message("⏳ Оплата проверяется. Квартира сохранена — номер появится здесь после подтверждения.");
    }} else if (data.status === "awaiting_receipt") {{
      message("После оплаты нажмите «Я оплатил(а)». Мы проверим поступление.");
    }} else if (data.status === "rejected") {{
      message("Оплата не подтверждена. Можно повторить оплату и отправить новый чек.");
    }} else {{
      message("");
      show("status", false);
    }}
  }}
  async function load() {{
    if (!initData || !startParam) {{
      message("Откройте это окно кнопкой под карточкой квартиры в Telegram.");
      show("refresh", false);
      return;
    }}
    try {{
      render(await api("/miniapp/api/session", {{}}));
    }}
    catch (error) {{ message(error.message); }}
  }}
  async function startPayment(plan, buttonId) {{
    if (paymentOpening) return;
    paymentOpening = true;
    const button = el(buttonId);
    const originalText = button.textContent;
    button.disabled = true;
    try {{
      const data = await api("/miniapp/api/start", {{plan}});
      render(data);
      if (tg) tg.openLink(data.payment_url); else location.href = data.payment_url;
    }} catch (error) {{ message(error.message); }}
    finally {{
      button.disabled = false;
      button.textContent = originalText;
      paymentOpening = false;
    }}
  }}
  el("pay-week").onclick = () => startPayment("week", "pay-week");
  el("pay-month").onclick = () => startPayment("month", "pay-month");
  el("check").onclick = async () => {{
    el("check").disabled = true;
    message("Проверяем заявку…");
    try {{
      render(await api("/miniapp/api/check", {{}}));
    }} catch (error) {{ message(error.message); }}
    finally {{ el("check").disabled = false; }}
  }};
  el("refresh").onclick = load;
  load();
  setInterval(() => {{ if (lastState === "pending") load(); }}, 5000);
}})();
</script>
</body>
</html>"""

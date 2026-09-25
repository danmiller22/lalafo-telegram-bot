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
  <script async src="https://telegram.org/js/telegram-web-app.js"></script>
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
    .terms {{ white-space: pre-line; line-height: 1.45; font-size: 14px; }}
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
    <div id="terms" class="terms hidden"></div>
    <div id="apartment" class="hidden">
      <div id="photos" class="photos"></div>
      <div id="details" class="details"></div>
    </div>
    <div id="status" class="status">Проверяем доступ…</div>
    <a id="phone" class="phone hidden"></a>
    <button id="pay-week" class="primary hidden">Оплатить неделю — {WEEK_PRICE} сом</button>
    <button id="pay-month" class="primary hidden">Оплатить месяц — {MONTH_PRICE} сом</button>
    <button id="access" class="secondary hidden">📞 Получить номер</button>
    <button id="accept" class="primary hidden">✅ Я ознакомлен(а) с условиями и согласен(на) со всеми пунктами</button>
    <button id="availability" class="secondary hidden">🔄 Проверить актуальность</button>
    <a id="privacy" class="button secondary hidden">🔒 Политика конфиденциальности</a>
    <a id="support" class="button secondary hidden">🛟 Техподдержка</a>
  </section>
  <div id="foot" class="foot">Номер виден только пользователю с подтверждённым доступом</div>
</main>
<script>
(() => {{
  const query = new URLSearchParams(location.search);
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const el = id => document.getElementById(id);
  let initData = query.get("tgWebAppData") || hash.get("tgWebAppData") || "";
  let startParam = query.get("tgWebAppStartParam") || hash.get("tgWebAppStartParam") || "";
  let paymentOpening = false;

  function telegramContext() {{
    const current = window.Telegram && window.Telegram.WebApp;
    if (!current) return null;
    if (current.initData) initData = current.initData;
    if (current.initDataUnsafe && current.initDataUnsafe.start_param) {{
      startParam = current.initDataUnsafe.start_param;
    }}
    current.ready();
    current.expand();
    return current;
  }}
  async function prepareTelegramContext() {{
    for (let attempt = 0; attempt < 20; attempt += 1) {{
      telegramContext();
      if (initData && startParam) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }}
  }}

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
    const approved = data.status === "approved";
    const accepted = Boolean(data.terms_accepted);
    el("title").textContent = approved ? "Квартира" : "Получить доступ";
    show("plans", !approved && accepted);
    show("terms", !approved && !accepted);
    show("apartment", approved);
    show("foot", !approved);
    show("status", !approved);
    show("phone", approved);
    const canPay = accepted && data.status !== "approved" && data.status !== "pending";
    show("pay-week", canPay);
    show("pay-month", canPay && Boolean(data.monthly_available));
    show("access", data.status === "awaiting_receipt" || data.status === "pending");
    show("accept", !approved && !accepted);
    show("availability", !approved);
    show("privacy", !approved);
    show("support", !approved);
    if (!approved) {{
      el("terms").textContent = data.terms_text || "";
      el("privacy").href = data.privacy_url || "#";
      el("support").href = data.support_url || "#";
    }}
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
      const room = apartment.rooms === "studio" ? "Студия" : (apartment.rooms || "—") + "-комнатная квартира";
      const author = apartment.author ? "\\n👤 Автор: " + apartment.author : "";
      el("details").textContent = "🏠 " + room + author + "\\n📍 " + (apartment.district || "Центр") + "\\n🏙 " + (apartment.city || "Бишкек") + "\\n💰 " + price + " сом" + deposit;
      if (apartment.description) el("details").textContent += "\\n\\n" + apartment.description;
      el("phone").textContent = "📞 " + data.phone;
      el("phone").href = "tel:" + String(data.phone || "").replace(/\\s+/g, "");
    }} else if (!accepted) {{
      message("Ознакомьтесь с условиями перед выбором тарифа.");
    }} else if (data.status === "pending" || data.status === "awaiting_receipt") {{
      message("После оплаты нажмите «Получить номер».");
    }} else if (data.status === "rejected") {{
      message("Откройте оплату повторно или выберите другой тариф.");
    }} else {{
      message("");
      show("status", false);
    }}
  }}
  async function load() {{
    await prepareTelegramContext();
    if (!initData || !startParam) {{
      message("Откройте это окно кнопкой под карточкой квартиры в Telegram.");
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
      const current = telegramContext();
      if (current) current.openLink(data.payment_url); else location.href = data.payment_url;
    }} catch (error) {{ message(error.message); }}
    finally {{
      button.disabled = false;
      button.textContent = originalText;
      paymentOpening = false;
    }}
  }}
  el("pay-week").onclick = () => startPayment("week", "pay-week");
  el("pay-month").onclick = () => startPayment("month", "pay-month");
  el("access").onclick = async () => {{
    el("access").disabled = true;
    message("Выдаём карточку…");
    try {{
      render(await api("/miniapp/api/access", {{}}));
    }} catch (error) {{ message(error.message); }}
    finally {{ el("access").disabled = false; }}
  }};
  el("accept").onclick = async () => {{
    el("accept").disabled = true;
    try {{ render(await api("/miniapp/api/consent", {{}})); }}
    catch (error) {{ message(error.message); }}
    finally {{ el("accept").disabled = false; }}
  }};
  el("availability").onclick = async () => {{
    el("availability").disabled = true;
    try {{ const data = await api("/miniapp/api/availability", {{}}); message(data.message); }}
    catch (error) {{ message(error.message); }}
    finally {{ el("availability").disabled = false; }}
  }};
  load();
}})();
</script>
</body>
</html>"""

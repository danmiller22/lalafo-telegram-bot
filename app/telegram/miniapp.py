from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl

from app.payment_plans import MONTH_PRICE, WEEK_PRICE
from app.terms import TERMS_TEXT


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


def mini_app_html(*, title: str = "Доступ к квартире", payment_urls: dict[str, str] | None = None) -> str:
    safe_title = escape(title)
    safe_terms = escape(TERMS_TEXT).replace("\n", "<br>")
    # Script-safe JSON: configured public checkout URLs are not credentials.
    checkout_json = json.dumps(payment_urls or {}, ensure_ascii=True).replace("<", "\\u003c")
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
    .status {{ border-radius: 13px; padding: 12px; margin: 12px 0; background: #12856a18; line-height: 1.4; }}
    .phone {{ font-size: 22px; font-weight: 800; color: #079b79; word-break: break-word; }}
    .photos {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin: 0 0 14px; }}
    .photos img {{ width: 100%; height: 118px; object-fit: cover; border-radius: 11px; }}
    .details {{ white-space: pre-line; font-size: 16px; font-weight: 650; line-height: 1.55; margin: 4px 0 12px; }}
    button, .button {{ width: 100%; border: 0; border-radius: 14px; padding: 14px 16px; margin-top: 9px; font: inherit; font-weight: 750; text-align: center; cursor: pointer; text-decoration: none; display: block; }}
    .primary {{ background: var(--tg-theme-button-color, #079b79); color: var(--tg-theme-button-text-color, white); }}
    .secondary {{ background: #12856a18; color: var(--tg-theme-link-color, #07866b); }}
    button:disabled {{ cursor: default; opacity: .45; }}
    label {{ display: block; font-size: 14px; line-height: 1.4; margin: 10px 0 16px; }}
    input[type=checkbox] {{ width: 20px; height: 20px; vertical-align: middle; }}
    details {{ font-size: 14px; line-height: 1.5; }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1 id="title">Получить доступ</h1>
    <div id="apartment" class="hidden">
      <div id="photos" class="photos"></div>
      <div id="details" class="details"></div>
    </div>
    <div id="status" class="status hidden"></div>
    <a id="phone" class="phone hidden"></a>
    <details id="agreement" class="hidden"><summary>Пользовательское соглашение</summary><p>{safe_terms}</p></details>
    <button id="pay-week" class="primary hidden">Недельный тариф — {WEEK_PRICE} сом</button>
    <label id="consent-week-row" class="hidden"><input id="consent-week" type="checkbox"> Согласен(на) с пользовательским соглашением</label>
    <button id="pay-month" class="primary hidden">Месячный тариф — {MONTH_PRICE} сом</button>
    <label id="consent-month-row" class="hidden"><input id="consent-month" type="checkbox"> Согласен(на) с пользовательским соглашением</label>
    <button id="reopen-payment" class="primary hidden">Открыть оплату</button>
    <input id="receipt-file" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" class="hidden">
    <button id="access" class="secondary hidden">Загрузить чек</button>
    <a id="privacy" class="button secondary hidden">🔒 Политика конфиденциальности</a>
  </section>
</main>
<script>
(() => {{
  const query = new URLSearchParams(location.search);
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const el = id => document.getElementById(id);
  let initData = query.get("tgWebAppData") || hash.get("tgWebAppData") || "";
  let startParam = query.get("tgWebAppStartParam") || hash.get("tgWebAppStartParam") || "";
  let paymentOpening = false;
  let paymentPoll = null;
  let paymentStart = null;
  let accessApproved = false;
  let selectedPlan = "week";
  const consentReady = {{week: false, month: false}};
  const paymentUrls = {checkout_json};

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
  function message(text) {{
    el("status").textContent = text;
    show("status", Boolean(text));
  }}
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
    // A response sent before access was granted must not undo the success screen.
    if (accessApproved && data.status !== "approved") return;
    const approved = data.status === "approved";
    const waiting = data.status === "awaiting_receipt" || data.status === "pending";
    if (data.plan === "week" || data.plan === "month") selectedPlan = data.plan;
    el("title").textContent = "Квартира";
    show("title", approved);
    show("apartment", approved);
    show("phone", approved);
    const canPay = !approved && !waiting;
    show("pay-week", canPay);
    show("pay-month", canPay && data.monthly_available !== false);
    show("access", waiting);
    show("reopen-payment", data.status === "awaiting_receipt");
    show("agreement", !approved && data.status !== "pending");
    show("consent-week-row", canPay || (data.status === "awaiting_receipt" && selectedPlan === "week"));
    show("consent-month-row", (canPay && data.monthly_available !== false) || (data.status === "awaiting_receipt" && selectedPlan === "month"));
    updateConsentButtons();
    show("privacy", !approved);
    if (!approved) {{
      el("privacy").href = data.privacy_url || "#";
    }}
    if (approved) {{
      accessApproved = true;
      if (paymentPoll) clearTimeout(paymentPoll);
      paymentPoll = null;
      message("");
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
    }} else if (data.status === "pending") {{
      message("Загрузите чек оплаты.");
      startPaymentPolling();
    }} else if (waiting) {{
      message("");
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
    if (paymentOpening || accessApproved || !consentReady[plan] || !el("consent-" + plan).checked) return;
    selectedPlan = plan;
    paymentOpening = true;
    const button = el(buttonId);
    const originalText = button.textContent;
    button.disabled = true;
    try {{
      const readyUrl = paymentUrls[plan];
      // Start recording the request, but do not delay a ready checkout link.
      paymentStart = api("/miniapp/api/start", {{plan}});
      if (readyUrl) {{
        const current = telegramContext();
        if (current) current.openLink(readyUrl); else location.href = readyUrl;
      }}
      const data = await paymentStart;
      render(data);
      startPaymentPolling();
      if (!readyUrl) {{
        const current = telegramContext();
        if (current) current.openLink(data.payment_url); else location.href = data.payment_url;
      }}
    }} catch (error) {{ message(error.message); }}
    finally {{
      updateConsentButtons();
      button.textContent = originalText;
      paymentOpening = false;
    }}
  }}
  function startPaymentPolling() {{
    if (paymentPoll || accessApproved) return;
    const poll = async () => {{
      try {{
        const data = await api("/miniapp/api/session", {{}});
        render(data);
        if (data.status === "approved" || data.status === "rejected") {{
          paymentPoll = null;
          return;
        }}
      }} catch (_) {{}}
      if (!accessApproved) paymentPoll = setTimeout(poll, 5000);
    }};
    paymentPoll = setTimeout(poll, 5000);
  }}
  document.addEventListener("visibilitychange", () => {{
    if (!document.hidden && !accessApproved) {{
      startPaymentPolling();
    }}
  }});
  function updateConsentButtons() {{
    for (const plan of ["week", "month"]) {{
      el("pay-" + plan).disabled = !consentReady[plan] || !el("consent-" + plan).checked;
    }}
    el("reopen-payment").disabled = !consentReady[selectedPlan] || !el("consent-" + selectedPlan).checked;
  }}
  for (const plan of ["week", "month"]) {{
    el("consent-" + plan).onchange = async () => {{
      consentReady[plan] = false;
      updateConsentButtons();
      if (!el("consent-" + plan).checked) return;
      try {{
        await api("/miniapp/api/consent", {{}});
        consentReady[plan] = el("consent-" + plan).checked;
      }} catch (error) {{
        el("consent-" + plan).checked = false;
        message(error.message);
      }}
      updateConsentButtons();
    }};
  }}
  el("pay-week").onclick = () => startPayment("week", "pay-week");
  el("pay-month").onclick = () => startPayment("month", "pay-month");
  el("reopen-payment").onclick = () => {{
    if (accessApproved || !consentReady[selectedPlan] || !el("consent-" + selectedPlan).checked) return;
    const url = paymentUrls[selectedPlan];
    if (url) {{
      const current = telegramContext();
      if (current) current.openLink(url); else location.href = url;
    }} else {{
      startPayment(selectedPlan, "reopen-payment");
    }}
  }};
  el("access").onclick = () => el("receipt-file").click();
  el("receipt-file").onchange = async () => {{
    const file = el("receipt-file").files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) {{ message("Размер файла — до 10 МБ."); return; }}
    el("access").disabled = true;
    message("Загрузка…");
    try {{
      const receiptData = await new Promise((resolve, reject) => {{
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
        reader.readAsDataURL(file);
      }});
      if (paymentStart) await paymentStart;
      render(await api("/miniapp/api/access", {{receipt_data: receiptData}}));
    }} catch (error) {{ message(error.message); }}
    finally {{
      el("access").disabled = false;
      el("receipt-file").value = "";
    }}
  }};
  load();
}})();
</script>
</body>
</html>"""

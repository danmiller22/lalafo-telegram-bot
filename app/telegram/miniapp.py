from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl

from app.payment_plans import WEEK_PRICE
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
    safe_terms = escape(TERMS_TEXT)
    # Script-safe JSON: configured public checkout URLs are not credentials.
    checkout_json = json.dumps(payment_urls or {}, ensure_ascii=True).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{safe_title}</title>
  <script async src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; --accent: #087f68; --muted: var(--tg-theme-hint-color, #64746e); --surface: var(--tg-theme-secondary-bg-color, #fff); }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--tg-theme-bg-color, #f3f6f5); color: var(--tg-theme-text-color, #172b24); }}
    main {{ max-width: 480px; margin: 0 auto; padding: 22px 16px calc(24px + env(safe-area-inset-bottom)); }}
    .brand {{ font-size: 14px; font-weight: 800; color: var(--tg-theme-link-color, #087f68); margin-bottom: 20px; }}
    h1 {{ font-size: 27px; line-height: 1.15; letter-spacing: -.6px; margin: 0 0 8px; }}
    .intro {{ font-size: 15px; color: var(--muted); line-height: 1.5; margin: 0 0 20px; }}
    .checkout {{ background: var(--surface); border: 1px solid #879b922b; border-radius: 18px; padding: 18px; margin: 0 0 14px; }}
    .caption {{ font-size: 14px; color: var(--muted); line-height: 1.4; margin: 6px 0 16px; }}
    button, .button {{ display: block; width: 100%; min-height: 52px; border: 0; border-radius: 12px; padding: 15px; font: inherit; font-size: 17px; font-weight: 700; text-align: center; text-decoration: none; cursor: pointer; touch-action: manipulation; }}
    .primary {{ background: var(--accent); color: #fff; }}
    .secondary {{ background: transparent; color: var(--tg-theme-link-color, #087f68); border: 1px solid #879b9260; }}
    button:disabled {{ cursor: default; background: #879b9230; color: var(--muted); }}
    button:focus-visible, a:focus-visible {{ outline: 3px solid #38a88b; outline-offset: 3px; }}
    .step {{ display: flex; align-items: center; gap: 9px; font-size: 17px; font-weight: 750; margin-bottom: 12px; }}
    .step span {{ display: grid; place-items: center; width: 25px; height: 25px; border-radius: 50%; background: #087f6818; color: var(--tg-theme-link-color, #087f68); font-size: 13px; }}
    .receipt-step {{ margin-top: 22px; }}
    .status {{ padding: 12px 14px; margin: 0 0 14px; border-radius: 12px; background: #087f6818; font-size: 15px; line-height: 1.4; }}
    .privacy {{ display: block; text-align: center; font-size: 13px; color: var(--muted); margin-top: 20px; text-decoration: none; padding: 8px; }}
    .phone {{ display: block; background: var(--accent); color: #fff; border-radius: 12px; padding: 16px; font-size: 22px; font-weight: 800; text-align: center; text-decoration: none; word-break: break-word; margin-top: 16px; }}
    .photos {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 16px; }}
    .photos img {{ width: 100%; height: 140px; object-fit: cover; border-radius: 12px; }}
    .details {{ white-space: pre-line; font-size: 16px; line-height: 1.6; }}
    .tariff-button {{ margin-bottom: 12px; }}
    #tariff-description {{ color: var(--tg-theme-text-color, #172b24); font-size: 26px; font-weight: 800; line-height: 1.3; }}
    .agreement-caption {{ font-size: 12px; font-weight: 400; color: var(--muted); line-height: 1.5; margin: 0 0 16px; }}
    .terms-link {{ color: var(--tg-theme-link-color, #087f68); }}
    .agreement-dialog {{ width: min(92vw, 460px); max-height: 82vh; border: 1px solid #879b9260; border-radius: 16px; padding: 20px; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #172b24); }}
    .agreement-text {{ white-space: pre-wrap; font-size: 15px; line-height: 1.5; margin-top: 16px; }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>
<main id="lifetime-checkout">
  <div class="brand">Arenda.KG</div>
  <h1 id="title" class="hidden"></h1>
  <p id="intro" class="intro"></p>
  <div id="status" class="status hidden" role="status" aria-live="polite"></div>
  <div id="tariff-description" class="intro hidden">
    Недельный доступ к контактам собственников — {WEEK_PRICE} сом
  </div>
  <p id="agreement-caption" class="agreement-caption hidden">Оплачивая доступ, вы подтверждаете, что ознакомились с <a id="terms-link" class="terms-link" href="#agreement">пользовательским договором</a> и соглашаетесь с его условиями.</p>
  <button id="pay-lifetime" class="primary tariff-button hidden">Оплатить {WEEK_PRICE} сом</button>
  <a id="privacy" class="privacy hidden">Политика конфиденциальности</a>
  <section id="checkout" class="checkout hidden">
    <div id="payment-step" class="step"><span>1</span>Оплата</div>
    <button id="reopen-payment" class="secondary hidden">Открыть Finik</button>
    <div class="step receipt-step"><span>2</span>Получение номера</div>
    <button id="access" class="primary hidden">Я оплатил(а)</button>
  </section>
  <div id="apartment" class="hidden">
    <div id="photos" class="photos"></div>
    <div id="details" class="details"></div>
  </div>
  <a id="phone" class="phone hidden"></a>
  <dialog id="agreement" class="agreement-dialog" aria-label="Пользовательский договор">
    <button id="close-agreement" class="secondary">Закрыть</button>
    <div class="agreement-text">{safe_terms}</div>
  </dialog>
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
  const paymentUrls = {checkout_json};
  let preparedUntil = Infinity;
  let checkoutPreparation = null;
  let refreshCheckoutTimer = null;

  function prepareCheckout() {{
    if (accessApproved || checkoutPreparation) return checkoutPreparation;
    if (paymentUrls.week && preparedUntil > Date.now()) return Promise.resolve();
    el("pay-lifetime").disabled = true;
    el("reopen-payment").disabled = true;
    checkoutPreparation = api("/miniapp/api/prepare", {{}}).then(data => {{
      if (data.status === "approved") {{ render(data); return; }}
      paymentUrls.week = data.payment_url;
      preparedUntil = data.expires_at_ms;
      el("pay-lifetime").disabled = false;
      el("reopen-payment").disabled = false;
      if (refreshCheckoutTimer) clearTimeout(refreshCheckoutTimer);
      refreshCheckoutTimer = setTimeout(() => {{
        paymentUrls.week = "";
        prepareCheckout();
      }}, Math.max(1000, preparedUntil - Date.now()));
    }}).catch(error => {{
      message(error.message);
      el("pay-lifetime").disabled = false;
      el("reopen-payment").disabled = false;
    }}).finally(() => {{ checkoutPreparation = null; }});
    return checkoutPreparation;
  }}

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
    const waiting = data.plan === "week" && (data.status === "awaiting_receipt" || data.status === "pending");
    selectedPlan = "week";
    el("title").textContent = approved ? "Квартира" : waiting ? "Оплата" : "";
    show("title", approved || waiting);
    el("intro").textContent = approved ? "" : waiting ? "7 дней · {WEEK_PRICE} сом" : "";
    show("intro", waiting);
    show("apartment", approved);
    show("phone", approved);
    const canPay = !approved && !waiting;
    show("tariff-description", canPay);
    show("checkout", waiting);
    show("privacy", !approved);
    show("agreement-caption", !approved);
    show("payment-step", data.status === "awaiting_receipt");
    show("pay-lifetime", canPay);
    show("access", waiting);
    show("reopen-payment", waiting && data.status === "awaiting_receipt");
    if (!approved) {{
      el("privacy").href = data.privacy_url || "#";
    }}
    if (approved) {{
      accessApproved = true;
      if (paymentPoll) clearTimeout(paymentPoll);
      paymentPoll = null;
      if (refreshCheckoutTimer) clearTimeout(refreshCheckoutTimer);
      refreshCheckoutTimer = null;
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
      el("details").textContent = "🏠 " + room + "\\n📍 " + (apartment.district || "Центр") + "\\n🏙 " + (apartment.city || "Бишкек") + "\\n💰 " + price + " сом" + deposit;
      if (apartment.description) el("details").textContent += "\\n\\n" + apartment.description;
      el("phone").textContent = "📞 " + data.phone;
      el("phone").href = "tel:" + String(data.phone || "").replace(/\\s+/g, "");
    }} else if (waiting && data.status === "pending") {{
      message("Оплата отправлена на проверку. Ожидайте подтверждения.");
      startPaymentPolling();
    }} else if (waiting) {{
      message("");
    }} else if (data.status === "rejected") {{
      message("Оплата не подтверждена. Проверьте перевод или обратитесь в поддержку.");
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
      const preparation = prepareCheckout();
      render(await api("/miniapp/api/session", {{}}));
      await preparation;
    }}
    catch (error) {{ message(error.message); }}
  }}
  async function startPayment(plan, buttonId) {{
    if (paymentOpening || accessApproved) return;
    selectedPlan = plan;
    paymentOpening = true;
    const button = el(buttonId);
    const originalText = button.textContent;
    button.disabled = true;
    try {{
      if (!paymentUrls[plan] || preparedUntil <= Date.now()) {{
        paymentUrls[plan] = "";
        await prepareCheckout();
      }}
      const readyUrl = paymentUrls[plan];
      // Start recording the request, but do not delay a ready checkout link.
      paymentStart = api("/miniapp/api/start", {{plan}});
      if (readyUrl) {{
        const current = telegramContext();
        if (current) current.openLink(readyUrl); else location.href = readyUrl;
      }}
      const data = await paymentStart;
      render(data);
      if (data.status === "approved") return;
      startPaymentPolling();
      if (!readyUrl) {{
        const current = telegramContext();
        if (current) current.openLink(data.payment_url); else location.href = data.payment_url;
      }}
    }} catch (error) {{ message(error.message); }}
    finally {{
      button.disabled = false;
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
      prepareCheckout();
      startPaymentPolling();
    }}
  }});
  el("terms-link").onclick = event => {{
    event.preventDefault();
    const agreement = el("agreement");
    if (agreement.showModal) agreement.showModal(); else agreement.setAttribute("open", "");
  }};
  el("close-agreement").onclick = () => {{
    const agreement = el("agreement");
    if (agreement.close) agreement.close(); else agreement.removeAttribute("open");
  }};
  el("pay-lifetime").onclick = () => startPayment("week", "pay-lifetime");
  el("reopen-payment").onclick = () => {{
    if (accessApproved) return;
    const url = paymentUrls[selectedPlan];
    if (url) {{
      const current = telegramContext();
      if (current) current.openLink(url); else location.href = url;
    }} else {{
      startPayment(selectedPlan, "reopen-payment");
    }}
  }};
  el("access").onclick = async () => {{
    el("access").disabled = true;
    try {{
      if (paymentStart) await paymentStart;
      render(await api("/miniapp/api/access", {{}}));
    }} catch (error) {{ message(error.message); }}
    finally {{ el("access").disabled = false; }}
  }};
  load();
}})();
</script>
</body>
</html>"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qsl

from app.payment_plans import WEEK_PRICE, LIFETIME_PRICE
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
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            return None
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
        if not isinstance(raw_user, dict):
            return None
        user_id = raw_user["id"]
        if type(user_id) is not int or user_id <= 0:
            return None
        first_name = str(raw_user.get("first_name") or "Пользователь")[:255]
        username_value = raw_user.get("username")
        username = str(username_value)[:64] if username_value else None
        return TelegramMiniAppUser(user_id, first_name, username)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def mini_app_html(*, title: str = "Получить доступ", payment_urls: dict[str, str] | None = None) -> str:
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
  <script id="telegram-sdk" async src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{ color-scheme: light; font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, system-ui, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; font-optical-sizing: auto; --accent: #116960; --muted: #737579; --surface: #fff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #fff; color: #141619; }}
    main {{ position: relative; max-width: 480px; margin: 0 auto; padding: 28px 18px calc(22px + env(safe-area-inset-bottom)); }}
    .back-button {{ position: absolute; top: 23px; left: 18px; width: auto; min-height: 30px; padding: 3px 0; border-radius: 6px; background: transparent; color: var(--accent); font-size: 14px; font-weight: 500; }}
    .brand {{ text-align: center; font-size: 18px; font-weight: 800; color: var(--accent); margin: 0 0 18px; }}
    h1 {{ text-align: center; font-size: 32px; line-height: 1.15; letter-spacing: -.8px; margin: 0 0 10px; }}
    h1, .brand, .plan-heading, .plan-price, #pay-now {{ font-family: "SF Pro Display", -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }}
    .intro {{ text-align: center; font-size: 16px; color: #66686b; line-height: 1.45; margin: 0 auto 28px; max-width: 350px; }}
    .checkout {{ border: 1px solid #ddd; border-radius: 14px; padding: 18px; margin: 18px 0; }}
    button, .button {{ display: block; width: 100%; min-height: 50px; border: 0; border-radius: 16px; padding: 14px; font: inherit; font-size: 19px; font-weight: 700; text-align: center; text-decoration: none; cursor: pointer; touch-action: manipulation; }}
    .primary {{ background: var(--accent); color: #fff; }}
    .secondary {{ background: transparent; color: var(--accent); border: 1px solid #ccd5d2; }}
    button:disabled {{ cursor: default; opacity: .55; }}
    button:focus-visible, a:focus-visible {{ outline: 3px solid #38a88b; outline-offset: 3px; }}
    .status {{ padding: 12px 14px; margin: 0 0 14px; border-radius: 12px; background: #edf6f3; font-size: 15px; line-height: 1.4; }}
    .privacy {{ display: block; text-align: center; font-size: 13px; color: #939599; margin: 22px 4px 0; border-top: 1px solid #e4e5e7; text-decoration: none; padding: 14px 0 4px; }}
    .phone {{ display: block; background: var(--accent); color: #fff; border-radius: 12px; padding: 16px; font-size: 22px; font-weight: 800; text-align: center; text-decoration: none; word-break: break-word; margin-top: 16px; }}
    .photos {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 16px; }}
    .photos img {{ width: 100%; height: 140px; object-fit: cover; border-radius: 12px; }}
    .details {{ white-space: pre-line; font-size: 16px; line-height: 1.6; }}
    .tariff-button {{ position: relative; display: block; text-align: left; padding: 20px; border: 1px solid #d7d8da; border-radius: 13px; background: #fff; color: #141619; margin: 0 0 14px; min-height: 120px; }}
    .tariff-button[aria-pressed="true"] {{ border: 1.5px solid var(--accent); background: #f0f7f6; }}
    .plan-heading {{ display: flex; align-items: center; gap: 12px; font-size: 17px; line-height: 1.3; padding-right: 68px; }}
    .plan-icon {{ width: 23px; height: 23px; flex: 0 0 23px; color: var(--accent); }}
    .plan-price {{ display: block; font-size: 32px; font-weight: 800; letter-spacing: -.6px; margin: 10px 0 2px; line-height: 1.1; }}
    .plan-note {{ display: block; color: #747679; font-size: 15px; font-weight: 400; line-height: 1.4; }}
    .popular {{ position: absolute; right: 12px; top: 12px; border-radius: 30px; background: var(--accent); color: #fff; padding: 5px 9px; font-size: 11px; font-weight: 500; }}
    .forever-offer {{ margin: 0; }}
    #pay-now {{ margin-top: 30px; font-size: 22px; }}
    .agreement-caption {{ text-align: center; font-size: 13px; font-weight: 400; color: #818387; line-height: 1.5; margin: 14px 10px 0; }}
    .terms-link {{ color: var(--accent); text-underline-offset: 2px; }}
    @media (max-width: 380px) {{ .plan-heading {{ font-size: 16px; gap: 10px; }} }}
    @media (max-width: 350px) {{ h1 {{ font-size: 28px; }} .plan-heading {{ font-size: 16px; gap: 8px; padding-right: 65px; }} .tariff-button {{ padding: 17px; }} .popular {{ font-size: 10px; padding: 5px 7px; right: 9px; }} }}
    .agreement-dialog {{ width: min(92vw, 460px); max-height: 82vh; border: 1px solid #879b9260; border-radius: 16px; padding: 20px; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #172b24); }}
    .agreement-text {{ white-space: pre-wrap; font-size: 15px; line-height: 1.5; margin-top: 16px; }}
    .hidden {{ display: none !important; }}
  </style>
</head>
<body>
<main id="lifetime-checkout">
  <button id="back" class="back-button" aria-label="Назад">← Назад</button>
  <div class="brand">Arenda.KG</div>
  <h1 id="title">Получить доступ</h1>
  <p id="intro" class="intro">Доступ ко всем контактам для прямой связи</p>
  <div id="status" class="status hidden" role="status" aria-live="polite"></div>
  <a id="other-apartments" class="button secondary hidden" href="https://t.me/bishkekarendakg">Смотреть актуальные квартиры</a>
  <div id="tariff-description">
    <button id="pay-lifetime" class="tariff-button" aria-pressed="true"{'' if payment_urls else ' disabled'}>
      <span class="popular">Популярный</span>
      <span class="plan-heading"><svg class="plan-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/></svg>Недельный доступ</span>
      <span class="plan-price">{WEEK_PRICE} сом</span><span class="plan-note">7 дней</span>
    </button>
  </div>
  <div id="forever-offer" class="forever-offer">
    <button id="pay-forever" class="tariff-button" aria-pressed="false">
      <span class="plan-heading"><svg class="plan-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 12c-2-3-3-5-5.5-5a5 5 0 0 0 0 10c2.5 0 3.5-2 5.5-5s3-5 5.5-5a5 5 0 0 1 0 10c-2.5 0-3.5-2-5.5-5Z"/></svg>Навсегда</span>
      <span class="plan-price">{LIFETIME_PRICE} сом</span><span class="plan-note">Без повторной оплаты</span>
    </button>
  </div>
  <button id="resume-payment" class="secondary hidden" style="margin-top:14px">Вернуться к оплате</button>
  <button id="pay-now" class="primary"{'' if payment_urls else ' disabled'}>Оплата</button>
  <p id="agreement-caption" class="agreement-caption">Оплачивая доступ, вы соглашаетесь с <a id="terms-link" class="terms-link" href="#agreement">пользовательским договором</a>.</p>
  <a id="privacy" class="privacy hidden">Политика конфиденциальности</a>
  <section id="checkout" class="checkout hidden">
    <button id="access" class="primary hidden">Я оплатил(а)</button>
    <button id="reopen-payment" class="secondary hidden" style="margin-top:10px">Открыть Finik ещё раз</button>
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
  let apartmentUnavailable = false;
  let sessionReady = false;
  let sessionLoading = false;
  let selectedPlan = "week";
  let lastSession = null;
  let tariffsView = false;
  let navigationVersion = 0;
  let paymentExpected = false;
  let pollingVersion = 0;
  let initializedWebApp = null;
  const paymentUrls = {checkout_json};
  const preparedUntil = {{week: Infinity, lifetime: Infinity}};
  const checkoutPreparation = {{}};
  const refreshCheckoutTimer = {{}};

  function prepareCheckout(plan = "week") {{
    if (accessApproved || checkoutPreparation[plan]) return checkoutPreparation[plan];
    if (paymentUrls[plan] && preparedUntil[plan] > Date.now()) return Promise.resolve();
    const buttonId = plan === "lifetime" ? "pay-forever" : "pay-lifetime";
    el(buttonId).disabled = true;
    el("reopen-payment").disabled = true;
    checkoutPreparation[plan] = api("/miniapp/api/prepare", {{plan}}).then(data => {{
      if (data.status === "approved" || data.status === "unavailable") {{ render(data); return; }}
      paymentUrls[plan] = data.payment_url;
      preparedUntil[plan] = data.expires_at_ms;
      el(buttonId).disabled = !paymentUrls[plan];
      el("reopen-payment").disabled = false;
      if (refreshCheckoutTimer[plan]) clearTimeout(refreshCheckoutTimer[plan]);
      refreshCheckoutTimer[plan] = setTimeout(() => {{
        paymentUrls[plan] = "";
      }}, Math.max(1000, preparedUntil[plan] - Date.now()));
    }}).catch(error => {{
      message(error.message);
      el(buttonId).disabled = !paymentUrls[plan];
      el("reopen-payment").disabled = false;
    }}).finally(() => {{ checkoutPreparation[plan] = null; }});
    return checkoutPreparation[plan];
  }}

  function telegramContext() {{
    const current = window.Telegram && window.Telegram.WebApp;
    if (current && current.initData) initData = current.initData;
    if (current && current.initDataUnsafe && current.initDataUnsafe.start_param) {{
      startParam = current.initDataUnsafe.start_param;
    }}
    if (!startParam && initData) {{
      startParam = new URLSearchParams(initData).get("start_param") || "";
    }}
    if (!current) return null;
    const nativeBack = current.BackButton && typeof current.BackButton.onClick === "function" && typeof current.BackButton.show === "function" && (!current.isVersionAtLeast || current.isVersionAtLeast("6.1"));
    if (initializedWebApp !== current) {{
      if (initializedWebApp && initializedWebApp.BackButton && initializedWebApp.BackButton.offClick) initializedWebApp.BackButton.offClick(goBack);
      current.ready();
      current.expand();
      if (nativeBack) {{
        current.BackButton.onClick(goBack);
        current.BackButton.show();
      }}
      initializedWebApp = current;
    }}
    show("back", !nativeBack);
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
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {{
      const response = await fetch(path, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{init_data: initData, start_param: startParam, ...body}}),
        signal: controller.signal
      }});
      const data = await response.json().catch(() => ({{detail: "Ошибка сервиса"}}));
      if (!response.ok) {{
        const error = new Error(data.detail || "Ошибка сервиса");
        error.status = response.status;
        throw error;
      }}
      return data;
    }} catch (error) {{
      if (controller.signal.aborted) throw new Error("Соединение прервалось. Попробуйте ещё раз.");
      throw error;
    }} finally {{ clearTimeout(timeout); }}
  }}
  function render(data) {{
    // A response sent before access was granted must not undo the success screen.
    if (accessApproved && data.status !== "approved") return;
    lastSession = data;
    const approved = data.status === "approved";
    apartmentUnavailable = data.status === "unavailable";
    show("other-apartments", apartmentUnavailable);
    const serverWaiting = (data.plan === "week" || data.plan === "lifetime") && (data.status === "awaiting_receipt" || data.status === "pending");
    if (serverWaiting) paymentExpected = true;
    if (approved || apartmentUnavailable || data.status === "rejected") {{ paymentExpected = false; pausePaymentPolling(); }}
    const waiting = serverWaiting && !tariffsView;
    if (waiting) selectedPlan = data.plan;
    el("title").textContent = approved ? "Квартира" : waiting ? "Оплата" : "Получить доступ";
    show("title", !apartmentUnavailable);
    el("intro").textContent = approved ? "" : waiting ? (selectedPlan === "lifetime" ? "Навсегда · {LIFETIME_PRICE} сом" : "7 дней · {WEEK_PRICE} сом") : "Доступ ко всем контактам для прямой связи";
    show("intro", !approved && !apartmentUnavailable);
    show("apartment", approved);
    show("phone", approved);
    const canPay = !approved && !waiting && !apartmentUnavailable;
    show("tariff-description", canPay);
    show("checkout", waiting);
    show("privacy", !approved);
    show("agreement-caption", !approved && !apartmentUnavailable);
    show("pay-lifetime", canPay);
    show("forever-offer", canPay);
    show("pay-forever", canPay);
    show("pay-now", canPay);
    show("resume-payment", serverWaiting && tariffsView && !approved);
    updatePlanSelection();
    show("access", waiting && data.status === "awaiting_receipt");
    show("reopen-payment", waiting && data.status === "awaiting_receipt");
    if (serverWaiting) startPaymentPolling();
    if (!approved) {{
      el("privacy").href = data.privacy_url || "#";
    }}
    if (approved) {{
      accessApproved = true;
      if (paymentPoll) clearTimeout(paymentPoll);
      paymentPoll = null;
      Object.values(refreshCheckoutTimer).forEach(clearTimeout);
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
    }} else if (apartmentUnavailable) {{
      message("Контакт этой квартиры сейчас недоступен. Выберите другой актуальный вариант.");
    }} else if (waiting && data.status === "pending") {{
      message("Заявка отправлена на проверку. Ожидайте подтверждения.");
      startPaymentPolling();
    }} else if (waiting) {{
      message("После оплаты нажмите «Я оплатил(а)».");
    }} else {{
      message("");
      show("status", false);
    }}
  }}
  async function load() {{
    if (sessionLoading || sessionReady || accessApproved) return;
    sessionLoading = true;
    try {{
      await prepareTelegramContext();
      if (!initData || !startParam) {{
        message("Откройте это окно кнопкой под карточкой квартиры в Telegram.");
        return;
      }}
      message("");
      const preparation = prepareCheckout();
      render(await api("/miniapp/api/session", {{}}));
      sessionReady = true;
      await preparation;
      el("pay-lifetime").disabled = false;
      el("pay-now").disabled = false;
    }}
    catch (error) {{
      if (error.status === 404) render({{status: "unavailable"}});
      else message(error.message);
    }}
    finally {{ sessionLoading = false; }}
  }}
  async function startPayment(plan, buttonId) {{
    if (paymentOpening || accessApproved || apartmentUnavailable || (!sessionReady && !paymentUrls[plan])) return;
    telegramContext();
    selectedPlan = plan;
    tariffsView = false;
    paymentExpected = true;
    const navigation = ++navigationVersion;
    let checkoutStarted = false;
    paymentOpening = true;
    const button = el(buttonId);
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Открываем Finik…";
    try {{
      if (!paymentUrls[plan] || preparedUntil[plan] <= Date.now()) {{
        paymentUrls[plan] = "";
        await prepareCheckout(plan);
      }}
      if (navigation !== navigationVersion || accessApproved || apartmentUnavailable) return;
      const readyUrl = paymentUrls[plan];
      // Persist the checkout before leaving Telegram. Opening Finik first can
      // suspend the WebView and lose the request, leaving no verification
      // button when the customer returns.
      checkoutStarted = true;
      paymentStart = api("/miniapp/api/start", {{plan}});
      const data = await paymentStart;
      render(data);
      if (data.status === "approved" || data.status === "unavailable" || data.status === "pending") return;
      startPaymentPolling();
      if (navigation !== navigationVersion) return;
      const targetUrl = readyUrl || data.payment_url;
      if (!targetUrl) throw new Error("Не удалось открыть оплату. Попробуйте ещё раз.");
      const current = telegramContext();
      if (current) current.openLink(targetUrl); else location.href = targetUrl;
    }} catch (error) {{
      message(error.message);
      // The server may have saved the checkout before the response was lost.
      startPaymentPolling();
    }}
    finally {{
      if (!checkoutStarted && !(lastSession && ["awaiting_receipt", "pending"].includes(lastSession.status))) {{ paymentExpected = false; pausePaymentPolling(); }}
      paymentStart = null;
      button.disabled = false;
      button.textContent = originalText;
      paymentOpening = false;
    }}
  }}
  function pausePaymentPolling() {{
    pollingVersion += 1;
    if (paymentPoll) clearTimeout(paymentPoll);
    paymentPoll = null;
  }}
  function startPaymentPolling() {{
    if (paymentPoll || accessApproved || apartmentUnavailable || !paymentExpected || document.hidden) return;
    const version = pollingVersion;
    const poll = async () => {{
      if (version !== pollingVersion || document.hidden) return;
      try {{
        const data = await api("/miniapp/api/session", {{track_open: false}});
        render(data);
        if (data.status === "approved" || data.status === "rejected" || data.status === "unavailable") {{
          if (version === pollingVersion) paymentPoll = null;
          return;
        }}
      }} catch (_) {{}}
      if (version === pollingVersion && paymentExpected && !accessApproved && !document.hidden) paymentPoll = setTimeout(poll, 5000);
    }};
    paymentPoll = setTimeout(poll, 5000);
  }}
  function goBack() {{
    const agreement = el("agreement");
    if (agreement.open) {{
      if (agreement.close) agreement.close(); else agreement.removeAttribute("open");
      return;
    }}
    const waiting = lastSession && ["awaiting_receipt", "pending"].includes(lastSession.status);
    if (!accessApproved && !tariffsView && (waiting || paymentOpening)) {{
      navigationVersion += 1;
      tariffsView = true;
      render(lastSession || {{status: "unpaid"}});
      return;
    }}
    const current = window.Telegram && window.Telegram.WebApp;
    if (current && typeof current.close === "function") current.close();
    else if (window.history && window.history.length > 1) window.history.back();
    else location.href = "https://t.me/bishkekarendakg";
  }}
  el("back").onclick = goBack;
  el("resume-payment").onclick = () => {{ tariffsView = false; if (lastSession) render(lastSession); }};
  document.addEventListener("visibilitychange", () => {{
    if (document.hidden) {{ pausePaymentPolling(); return; }}
    if (!accessApproved) {{
      if (!sessionReady) {{ load(); return; }}
      if (paymentExpected) startPaymentPolling();
    }}
  }});
  el("telegram-sdk").addEventListener("load", () => {{
    telegramContext();
    load();
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
  function updatePlanSelection() {{
    el("pay-lifetime").setAttribute("aria-pressed", String(selectedPlan === "week"));
    el("pay-forever").setAttribute("aria-pressed", String(selectedPlan === "lifetime"));
  }}
  function selectPlan(plan) {{
    if (accessApproved || apartmentUnavailable || paymentOpening) return;
    selectedPlan = plan;
    updatePlanSelection();
  }}
  el("pay-lifetime").onclick = () => selectPlan("week");
  el("pay-forever").onclick = () => selectPlan("lifetime");
  el("pay-now").onclick = () => startPayment(selectedPlan, "pay-now");
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
